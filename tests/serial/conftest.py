import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess

import pytest


PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repo with spaces'
    for name in ('.codex/python.sh', '.codex/share_check.py', '.githooks/pre-commit', '.githooks/pre-push', 'requirements-dev.lock'):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / name, target)
    (root / '.githooks/pre-commit').chmod(0o755)
    (root / '.githooks/pre-push').chmod(0o755)
    cache = tmp_path / 'cache'
    key = hashlib.sha256((root / 'requirements-dev.lock').read_bytes()).hexdigest()[:16]
    binary = cache / 'oa-minimal' / f'python311-{platform.machine()}-{key}' / 'bin'
    binary.mkdir(parents=True)
    for name, body in {
        'python': 'printf "%s\\n" "$*" >> "$CHECK_CALLS"\ncase "$*" in *ruff*) exit "${RUFF_STATUS:-0}";; *pytest*) exit "${TEST_STATUS:-0}";; *) exit "${SHARE_STATUS:-0}";; esac\n',
        'uv': 'exit 0\n',
    }.items():
        path = binary / name
        path.write_text('#!/usr/bin/env bash\n' + body)
        path.chmod(0o755)
    inherited = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
    environment = {**inherited, 'XDG_CACHE_HOME': str(cache), 'CHECK_CALLS': str(tmp_path / 'calls'),
                   'PATH': str(binary) + os.pathsep + os.environ['PATH']}

    def run(*args, input=None, **overrides):
        return subprocess.run(args, cwd=root, env={**environment, **overrides}, input=input, text=True, capture_output=True)

    assert run('git', 'init', '-q').returncode == 0
    assert run('git', 'config', 'user.name', 'Synthetic test').returncode == 0
    assert run('git', 'config', 'user.email', 'synthetic@example.invalid').returncode == 0
    assert run('git', 'add', '.').returncode == 0
    assert run('git', 'commit', '-qm', 'fixture').returncode == 0
    return root, run, Path(environment['CHECK_CALLS'])


