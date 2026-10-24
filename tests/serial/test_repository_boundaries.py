import io
from pathlib import Path
import shlex
import sys

import pytest

from apps.oa import cli
from tests.serial.test_quality_entry import repository


@pytest.mark.parametrize('name,content,kind', [
    ('.env', 'OA_DB_PASSWORD=synthetic', 'private_path'),
    ('runtime/report.json', '{}', 'private_path'),
    ('cookies-current.json', '{}', 'private_path'),
    ('receipt.pdf', 'synthetic receipt', 'private_path'),
    ('tests/fixtures/legacy-workbook.xls', 'unreviewed replacement', 'private_path'),
    ('example.py', 'value = "' + 'sk-' + 'A1' * 20 + '"', 'api_key'),
    ('example.py', 'value = "-----BEGIN ' + 'PRIVATE KEY-----"', 'private_key'),
])
def test_staged_sensitive_files_are_blocked_without_printing_values(repository, name, content, kind):
    root, run, _ = repository
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    assert run('git', 'add', name).returncode == 0
    result = run(sys.executable, '.codex/share_check.py')
    assert result.returncode == 1
    assert kind + ': ' + name in result.stdout
    assert content not in result.stdout


def test_deleted_secret_is_still_blocked_in_outgoing_history(repository):
    root, run, _ = repository
    secret = 'sk-' + 'A1' * 20
    target = root / 'deleted.py'
    target.write_text('key = "' + secret + '"\n')
    assert run('git', 'add', 'deleted.py').returncode == 0
    assert run('git', 'commit', '-qm', 'synthetic history').returncode == 0
    assert run('git', 'rm', 'deleted.py').returncode == 0
    assert run('git', 'commit', '-qm', 'delete fixture').returncode == 0
    assert run(sys.executable, '.codex/share_check.py').returncode == 0
    history = run(sys.executable, '.codex/share_check.py', '--history', 'HEAD')
    assert history.returncode == 1
    assert 'api_key: deleted.py' in history.stdout
    assert secret not in history.stdout
    head = run('git', 'rev-parse', 'HEAD').stdout.strip()
    update = f'refs/heads/main {head} refs/heads/main {"0" * 40}\n'
    deletion = f'(delete) {"0" * 40} refs/heads/retired {head}\n'
    for records in (update, deletion + update, update + deletion):
        pushed = run(sys.executable, '.codex/share_check.py', '--push', input=records)
        assert pushed.returncode == 1
        assert 'api_key: deleted.py' in pushed.stdout
        assert secret not in pushed.stdout


@pytest.mark.parametrize('mixed', [False, True])
def test_pre_push_allows_branch_deletion_without_scanning_staged_files(repository, tmp_path, mixed):
    root, run, _ = repository
    remote = tmp_path / 'remote.git'
    assert run('git', 'init', '--bare', '-q', str(remote)).returncode == 0
    assert run('git', 'push', str(remote), 'HEAD:refs/heads/retired').returncode == 0
    (root / '.env').write_text('OA_DB_PASSWORD=synthetic\n')
    assert run('git', 'add', '.env').returncode == 0
    setup = run('bash', '.codex/python.sh', 'setup')
    assert setup.returncode == 0
    Path(setup.stdout.strip()).write_text('#!/usr/bin/env bash\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    local_ref = run('git', 'symbolic-ref', 'HEAD').stdout.strip()
    refspecs = [':refs/heads/retired', *([f'{local_ref}:refs/heads/kept'] if mixed else [])]
    result = run('git', 'push', str(remote), *refspecs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert run('git', 'ls-remote', str(remote), 'refs/heads/retired').stdout == ''
    kept = run('git', 'ls-remote', str(remote), 'refs/heads/kept').stdout
    assert bool(kept) is mixed


@pytest.mark.parametrize('local_ref,deleting,remote_ref', [
    ('refs/tags/release', False, 'refs/tags/release'),
    ('refs/snapshots/private', False, 'refs/heads/main'),
    ('(delete)', True, 'refs/tags/release'),
    ('(delete)', False, 'refs/heads/main'),
])
def test_pre_push_keeps_nonbranch_refs_blocked(repository, local_ref, deleting, remote_ref):
    _, run, _ = repository
    head = run('git', 'rev-parse', 'HEAD').stdout.strip()
    local_sha = '0' * 40 if deleting else head
    records = f'{local_ref} {local_sha} {remote_ref} {head}\n'
    result = run(sys.executable, '.codex/share_check.py', '--push', input=records)
    assert result.returncode == 1
    assert 'private_git_ref: only branch refs may be shared' in result.stdout


def test_pre_push_failure_prevents_remote_update(repository, tmp_path):
    _, run, calls = repository
    remote = tmp_path / 'remote.git'
    assert run('git', 'init', '--bare', '-q', str(remote)).returncode == 0
    assert run('bash', '.codex/python.sh', 'setup').returncode == 0
    result = run('git', 'push', str(remote), 'HEAD:refs/heads/main', SHARE_STATUS='1')
    assert result.returncode != 0
    assert run('git', 'ls-remote', str(remote), 'refs/heads/main').stdout == ''
    assert '-B .codex/share_check.py --push' in calls.read_text().splitlines()


@pytest.mark.parametrize('state,error', [('branch', 'production_main_required'),
                                        ('dirty', 'production_checkout_dirty'),
                                        ('ahead', 'production_main_not_synced'),
                                        ('remote_ahead', 'production_main_not_synced'),
                                        ('unpublished', 'production_main_unpublished')])
def test_production_requires_clean_published_main(repository, monkeypatch, state, error):
    root, run, _ = repository
    remote = root.parent / 'origin.git'
    assert run('git', 'branch', '-M', 'main').returncode == 0
    assert run('git', 'init', '--bare', '-q', str(remote)).returncode == 0
    assert run('git', 'remote', 'add', 'origin', str(remote)).returncode == 0
    assert run('git', 'push', '-q', 'origin', 'main').returncode == 0
    monkeypatch.setenv('GIT_DIR', str(root.parent / 'different.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(root.parent / 'different-checkout'))
    cli.require_production_main(root)
    if state == 'branch':
        assert run('git', 'switch', '-c', 'codex/test').returncode == 0
    elif state == 'dirty':
        (root / 'README.md').write_text('dirty\n')
    elif state == 'ahead':
        assert run('git', 'commit', '--allow-empty', '-qm', 'ahead').returncode == 0
    elif state == 'remote_ahead':
        previous = run('git', 'rev-parse', 'HEAD').stdout.strip()
        next_commit = run('git', 'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'remote advancement')
        assert next_commit.returncode == 0
        assert run('git', 'push', '-q', 'origin', next_commit.stdout.strip() + ':refs/heads/main').returncode == 0
        assert run('git', 'update-ref', 'refs/remotes/origin/main', previous).returncode == 0
    else:
        assert run('git', 'remote', 'set-url', 'origin', str(root.parent / 'missing.git')).returncode == 0
    with pytest.raises(ValueError, match=error):
        cli.require_production_main(root)


def test_production_guard_stops_before_api_or_queue_creation(tmp_path, monkeypatch, capsys):
    from apps.oa.service import Approval

    def refuse():
        raise ValueError('production_main_required')

    monkeypatch.setattr(cli, 'require_production_main', refuse)
    monkeypatch.setattr(Approval, '_api', lambda self: pytest.fail('API must not be reached'))
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"operation":"finish"}\n'))
    root = tmp_path / 'runtime'
    assert cli.main(['--root', str(root), 'pending']) == 1
    assert 'production_main_required' in capsys.readouterr().out
    assert not (root / 'approval.sqlite3').exists()
