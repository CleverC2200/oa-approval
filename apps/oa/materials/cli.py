from __future__ import annotations

import argparse
import json
from pathlib import Path


def _parser():
    parser = argparse.ArgumentParser(description='Prepare, read, recognize or compress complete local material artifacts.')
    commands = parser.add_subparsers(dest='operation', required=True)
    prepare = commands.add_parser('prepare', help='Safely expand and prepare local originals into a new directory.')
    source = prepare.add_mutually_exclusive_group(required=True)
    source.add_argument('--file', type=Path, action='append', dest='files')
    source.add_argument('--manifest', type=Path)
    prepare.add_argument('--output-dir', type=Path, required=True)
    read = commands.add_parser('read', help='Read the complete material text without changing files.')
    read.add_argument('material_file', type=Path)
    read.add_argument('--format', choices=('text', 'json'), default='text')
    recognize = commands.add_parser('recognize', help='Read attachments through the authorized model interface.')
    source = recognize.add_mutually_exclusive_group(required=True)
    source.add_argument('--file', action='append', type=Path, dest='files')
    source.add_argument('--material-file', type=Path)
    recognize.add_argument('--resource-id', help='Real resource ID bound to the frozen material.')
    recognize.add_argument('--page', type=int)
    recognize.add_argument('--prompt')
    recognize.add_argument('--output-dir', type=Path)
    recognize.add_argument('--timeout-seconds', type=float, default=90)
    recognize.add_argument('--budget-seconds', type=float, default=1800)
    recognize.add_argument('--region', type=int, nargs=4, metavar=('X0', 'Y0', 'X1', 'Y1'))
    recognize.add_argument('--rotate', type=int, choices=(0, 90, 180, 270), default=0)
    compress = commands.add_parser('compress', help='Create a bounded reading copy while preserving the source.')
    compress.add_argument('--file', type=Path, required=True)
    compress.add_argument('--output-dir', type=Path)
    compress.add_argument('--max-dimension', type=int, default=8192)
    compress.add_argument('--max-pixels', type=int, default=16_000_000)
    compress.add_argument('--max-bytes', type=int, default=8 * 1024 * 1024)
    compress.add_argument('--quality', type=int, default=90)
    compress.add_argument('--timeout-seconds', type=float, default=60)
    return parser


def _recognize(args):
    from apps.oa.materials import recognize

    values = vars(args).copy()
    values.pop('operation')
    if values['prompt'] is None:
        values['prompt'] = recognize.DEFAULT_PROMPT
    try:
        report = recognize.recognize(**values)
        print(json.dumps({key: report[key] for key in ('status', 'report_file', 'model', 'transport')}
                         | {'parts': len(report['parts']), 'skipped_sources': len(report.get('skipped_sources', [])),
                            'coverage': report.get('coverage'), 'reason': report.get('reason'),
                            'selection': report.get('selection'),
                            'observations_file': report.get('observations_file'), 'error_code': report.get('error_code')}, ensure_ascii=False))
        return 0 if report['status'] in {'complete', 'skipped'} else 1
    except (OSError, ValueError) as error:
        code = str(error) if str(error) in {'attachment_api_key_missing', 'attachment_recognition_input_invalid', 'attachment_recognition_timeout_invalid', 'attachment_region_invalid', 'attachment_region_source_invalid', 'attachment_rotation_invalid', 'attachment_resource_invalid', 'attachment_page_invalid'} else 'attachment_recognition_start_failed'
        print(json.dumps({'status': 'failed', 'error_code': code}))
        return 1


def _compress(args):
    from apps.oa.materials import compress

    result = compress.compress_image(args.file, output_dir=args.output_dir, max_dimension=args.max_dimension,
        max_pixels=args.max_pixels, max_bytes=args.max_bytes, quality=args.quality, timeout_seconds=args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'complete' else 1


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.operation == 'recognize':
        return _recognize(args)
    if args.operation == 'compress':
        return _compress(args)
    try:
        if args.operation == 'read':
            from apps.oa.materials.read import read

            text = read(args.material_file)
            if args.format == 'text':
                print(text, end='')
            else:
                print(json.dumps({'status': 'complete', 'material_file': str(args.material_file.absolute()),
                                  'text': text}, ensure_ascii=False))
            return 0
        from apps.oa.materials.prepare import prepare

        result = prepare(files=args.files, manifest=args.manifest, output_dir=args.output_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'complete' else 1
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        code = str(error)
        allowed = ('attachment_', 'material_')
        print(json.dumps({'status': 'failed', 'error_code': code if code.startswith(allowed)
                          else f'material_{args.operation}_failed'}))
        return 1
