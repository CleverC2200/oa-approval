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


@pytest.mark.parametrize('status', [0, 7])
def test_check_runs_existing_commands_and_propagates_failure(repository, status):
    _, run, calls = repository
    result = run('bash', '.codex/python.sh', 'check', TEST_STATUS=str(status))
    assert result.returncode == status
    assert calls.read_text().splitlines() == ['-B .codex/share_check.py', '-B -m ruff check apps tests', '-B -m pytest -q']
    calls.unlink()
    assert run('bash', '.codex/python.sh', 'check', RUFF_STATUS='9').returncode == 9
    assert calls.read_text().splitlines() == ['-B .codex/share_check.py', '-B -m ruff check apps tests']
    calls.unlink()
    assert run('bash', '.codex/python.sh', 'check', SHARE_STATUS='8').returncode == 8
    assert calls.read_text().splitlines() == ['-B .codex/share_check.py']


@pytest.mark.parametrize('relative', ['apps/sample.py', '.codex/environments/environment.toml'])
def test_setup_installs_hook_and_failed_check_blocks_commit(repository, relative):
    root, run, calls = repository
    assert run('bash', '.codex/python.sh', 'setup').returncode == 0
    assert run('git', 'config', '--get', 'core.hooksPath').stdout.strip() == '.githooks'
    (root / 'README.md').write_text('documentation\n')
    assert run('git', 'add', 'README.md').returncode == 0
    assert run('git', 'commit', '-qm', 'docs').returncode == 0
    assert calls.read_text().splitlines() == ['-B .codex/share_check.py']
    calls.unlink()
    source = root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('value = 1\n')
    assert run('git', 'add', relative).returncode == 0
    assert run('git', 'commit', '-qm', 'code', RUFF_STATUS='4').returncode != 0
    assert run('git', 'log', '-1', '--format=%s').stdout.strip() == 'docs'
    calls.unlink()
    source.write_text('value = 2\n')
    assert run('git', 'commit', '-qm', 'partial').returncode == 1
    assert not calls.exists()
    assert run('git', 'add', relative).returncode == 0
    assert run('git', 'commit', '-qm', 'checked').returncode == 0
    assert calls.read_text().splitlines() == ['-B .codex/share_check.py', '-B -m ruff check apps tests', '-B -m pytest -q']


def test_setup_preserves_existing_custom_hook_path(repository):
    _, run, _ = repository
    assert run('git', 'config', 'core.hooksPath', 'custom-hooks').returncode == 0
    assert run('bash', '.codex/python.sh', 'setup').returncode == 0
    assert run('git', 'config', '--get', 'core.hooksPath').stdout.strip() == 'custom-hooks'
