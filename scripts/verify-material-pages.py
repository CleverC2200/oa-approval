import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description='Verify bounded material CLI transport and source preservation; this does not perform a business audit.')
    parser.add_argument('--material-file', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    source = args.material_file.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    command = [sys.executable, '-m', 'apps.oa.materials', 'read', str(source), '--format', 'json']
    project = Path(__file__).resolve().parents[1]

    def call(*arguments):
        result = subprocess.run([*command, *arguments], cwd=project, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr + result.stdout[:200]
        return json.loads(result.stdout)

    expected = call()['text']
    offset, chunks, ranges = 0, [], []
    while offset is not None:
        page = call('--page-offset', str(offset), '--expected-sha256', digest)
        assert page['material_sha256'] == digest and page['coverage'] == 'range_only'
        assert page['offset'] == offset and len(page['text']) <= 2000
        assert page['end'] == offset + len(page['text'])
        (output / f'page-{len(chunks):04}.json').write_text(json.dumps(page, ensure_ascii=False, indent=2))
        chunks.append(page['text'])
        ranges.append([offset, page['end']])
        offset = page['next_offset']
    assert ''.join(chunks) == expected
    assert source.read_bytes() == raw
    proof = {'material_file': str(source), 'material_sha256': digest, 'pages': len(chunks),
             'total_chars': len(expected), 'max_page_chars': max(map(len, chunks)),
             'ranges': ranges, 'reconstructed_matches_full': True, 'source_unchanged': True,
             'proves_read_transport_only': True, 'business_audit_completed': False}
    (output / 'proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2))
    print(json.dumps({key: proof[key] for key in ['pages', 'total_chars', 'max_page_chars',
                                               'reconstructed_matches_full', 'source_unchanged']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
