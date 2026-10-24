from __future__ import annotations

import argparse
import json
from pathlib import Path
from zipfile import BadZipFile

from apps.oa.runtime import control


def main(argv=None):
    parser = argparse.ArgumentParser(description='Import approved local reference data.')
    parser.add_argument('--root', type=Path, default=None)
    commands = parser.add_subparsers(dest='operation', required=True)
    tpm = commands.add_parser('import-tpm', help='Import the approved TPM price workbook into the selected root.')
    tpm.add_argument('source', type=Path)
    tpm.add_argument('--runtime-root', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.root is not None and args.runtime_root is not None:
        parser.error('use --root or --runtime-root, not both')
    from apps.oa.references.tpm import import_workbook

    try:
        result = import_workbook(args.source, root=args.root or args.runtime_root or control.RUNTIME_ROOT)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, BadZipFile):
        print(json.dumps({'status': 'failed', 'error_code': 'tpm_price_reference_import_failed'}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
