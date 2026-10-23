from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {
    'tests/fixtures/legacy-workbook.xls': '8ba6fb64229087701d5f27b5e0ce6301f72386f0f0c14f12f06c2756ee4549af',
}
PRIVATE_DIRECTORIES = {'runtime', '.scratch', 'attachments', 'prepared', 'reports', 'exports', 'downloads', 'screenshots', 'recordings'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore', '.db', '.sqlite', '.sqlite3',
                    '.har', '.pdf', '.png', '.jpg', '.jpeg', '.heic', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.rar', '.7z'}
TOKENS = {
    'private_key': rb'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----',
    'github_token': rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})',
    'api_key': rb'sk-(?:proj-)?[A-Za-z0-9_-]{20,}',
    'cloud_key': rb'AKIA[0-9A-Z]{16}',
    'credential_uri': rb'(?:postgres(?:ql)?|mysql|mssql)://[^:/\s]+:[^@\s]+@',
}
KEY_NAMES = rb'(?:OA_APP_SECRET|OA_DB_PASSWORD|OA_REVIEW_API_KEY|OPENAI_API_KEY|DEEPSEEK_API_KEY)'
QUOTED_KEYS = re.compile(KEY_NAMES + rb'''["']?[ \t]*[:=][ \t]*["']([^"'\r\n]+)''', re.I)
BARE_KEYS = re.compile(rb'^[ \t]*(?:export[ \t]+)?' + KEY_NAMES + rb'[ \t]*=[ \t]*([^\r\n#]+)', re.I | re.M)


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def private_path(name):
    path = PurePosixPath(name)
    return (bool(set(path.parts[:-1]) & PRIVATE_DIRECTORIES)
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or (path.name.startswith('.env') and name != '.env.example')
            or re.match(r'(?:credentials|service-account|storage[-_]state|cookies).*\.json$', path.name, re.I) is not None
            or path.name.endswith(('.db-wal', '.db-shm')))


def secret_kinds(content, name=''):
    found = {name for name, pattern in TOKENS.items() if re.search(pattern, content)}
    patterns = (QUOTED_KEYS,) if name.endswith('.py') else (QUOTED_KEYS, BARE_KEYS)
    for pattern in patterns:
        for match in pattern.finditer(content):
            value = match[1].strip().strip(b'"\'')
            if (not value or value.startswith((b'$', b'<'))
                    or re.fullmatch(rb'(?:fake|test|demo|dummy|example|placeholder)(?:[-_][A-Za-z0-9_-]+)?', value, re.I)):
                continue
            if len(value) >= 20 and re.search(rb'[A-Za-z]', value) and re.search(rb'[0-9]', value):
                found.add('credential_value')
    return found


def blobs(entries):
    payload = b''.join(oid.encode() + b'\n' for oid, _ in entries)
    result = subprocess.run(['git', 'cat-file', '--batch'], cwd=ROOT, input=payload, capture_output=True, check=True)
    position = 0
    for _, name in entries:
        end = result.stdout.index(b'\n', position)
        _, kind, size = result.stdout[position:end].split()
        start = end + 1
        position = start + int(size) + 1
        if kind == b'blob':
            yield name, result.stdout[start:position - 1]


def check(revisions):
    entries, names = [], set()
    if revisions:
        for revision in revisions:
            names.update(git('log', '--format=', '--name-only', revision).decode().splitlines())
            for line in git('rev-list', '--objects', revision).decode().splitlines():
                if ' ' in line:
                    oid, name = line.split(' ', 1)
                    entries.append((oid, name))
    else:
        for line in git('ls-files', '--stage', '-z').decode().split('\0'):
            if line:
                metadata, name = line.split('\t', 1)
                entries.append((metadata.split()[1], name))
                names.add(name)
    findings = {(name, 'private_path') for name in names if name and name not in FIXTURES and private_path(name)}
    for name, content in blobs(list(dict.fromkeys(entries))):
        if name in FIXTURES and hashlib.sha256(content).hexdigest() != FIXTURES[name]:
            findings.add((name, 'private_path'))
        findings.update((name, kind) for kind in secret_kinds(content, name))
    for name, kind in sorted(findings):
        print(f'{kind}: {name}')
    return 1 if findings else 0


def main():
    parser = argparse.ArgumentParser(description='Check staged files or outgoing Git history without printing secret values.')
    parser.add_argument('--history', nargs='+')
    parser.add_argument('--push', action='store_true')
    args = parser.parse_args()
    revisions = args.history
    if args.push:
        revisions = []
        for line in sys.stdin:
            local_ref, local_sha, _, _ = line.split()
            if not local_ref.startswith('refs/heads/'):
                print('private_git_ref: only branch refs may be shared')
                return 1
            if set(local_sha) != {'0'}:
                revisions.append(local_sha)
        if not revisions:
            return 0
    return check(revisions)


if __name__ == '__main__':
    raise SystemExit(main())
