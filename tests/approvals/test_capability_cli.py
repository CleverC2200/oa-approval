import json
from pathlib import Path
import subprocess
import sys

from apps.oa.approvals import cli
from apps.oa.approvals.demo import create_demo


PROJECT = Path(__file__).resolve().parents[2]


def test_status_and_show_do_not_create_a_missing_root(tmp_path, capsys):
    root = tmp_path / 'missing'
    for operation in (['status'], ['show', '--task-id', '1']):
        assert cli.main(['--root', str(root), *operation]) == 0
        assert json.loads(capsys.readouterr().out)['status'] == 'not_initialized'
        assert not root.exists()


def test_status_and_show_read_closed_facts_without_mutation(tmp_path, capsys):
    app = create_demo(tmp_path)
    started = app.pending()
    run_id = started['run_id']
    task_id = started['tasks'][0]['id']
    app.close(run_id)
    app.store.connection.close()
    files = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert cli.main(['--root', str(tmp_path), 'status']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['run']['id'] == run_id
    assert status['run']['status'] == 'closed'
    assert status['owner']['reachable'] is False
    assert status['summary'] == {'queued': 3}
    assert cli.main(['--root', str(tmp_path), 'show', '--task-id', str(task_id)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown['task']['id'] == task_id
    assert shown['reviews'] == shown['submissions'] == []
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file() and not path.name.endswith(('-wal', '-shm'))} == files


def test_finish_is_bound_to_current_run_and_closes_through_public_cli(tmp_path):
    process = subprocess.Popen([sys.executable, '-m', 'apps.oa.approvals', '--root', str(tmp_path), 'start', '--demo'],
                               cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline()
        assert line, process.stderr.read()
        started = json.loads(line)
        base = [sys.executable, '-m', 'apps.oa.approvals', '--root', str(tmp_path)]
        wrong = subprocess.run([*base, 'finish', '--run-id', 'wrong'], cwd=PROJECT, capture_output=True, text=True, timeout=5)
        assert wrong.returncode == 1
        assert json.loads(wrong.stdout)['error'] == 'run_mismatch'
        assert process.poll() is None
        status = subprocess.run([*base, 'status'], cwd=PROJECT, capture_output=True, text=True, timeout=5)
        assert json.loads(status.stdout)['owner'] == {'reachable': True, 'run_id': started['run_id']}
        finished = subprocess.run([*base, 'finish', '--run-id', started['run_id']], cwd=PROJECT, capture_output=True, text=True, timeout=10)
        assert finished.returncode == 0, finished.stderr + finished.stdout
        assert json.loads(finished.stdout)['status'] == 'closed'
        process.wait(timeout=5)
        assert process.returncode == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_stdin_null_reports_failure_and_continues_to_the_next_request(tmp_path):
    result = subprocess.run(
        [sys.executable, '-m', 'apps.oa.approvals', '--root', str(tmp_path), 'start', '--demo'],
        cwd=PROJECT, input='null\n{"operation":"status"}\n{"operation":"finish"}\n',
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    started, failed, status = [json.loads(line) for line in result.stdout.splitlines()]
    assert failed == {'status': 'failed', 'error': 'AttributeError'}
    assert status == {'run_id': started['run_id'], 'status': 'running'}
    assert not Path(started['socket']).exists()


def test_help_does_not_import_owner_collection_or_external_adapters():
    script = '''import importlib.abc
import runpy
import sys

class LocalHelpOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'apps.oa.approvals.owner', 'apps.oa.approvals.operations'} or fullname.startswith(('apps.oa.approvals.sources', 'apps.oa.approvals.collection')):
            raise AssertionError('help imported approval runtime or external adapters')

sys.meta_path.insert(0, LocalHelpOnly())
sys.argv = ['approvals', '--help']
runpy.run_module('apps.oa.approvals', run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=PROJECT, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'start' in result.stdout and 'preview' in result.stdout
