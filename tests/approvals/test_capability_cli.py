import json
from pathlib import Path
import subprocess
import sys

import pytest

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


def test_batch_status_separates_frozen_prefix_from_outside_queue(tmp_path, capsys):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=2)
    run_id = started['run_id']
    first, second, outside = [task['id'] for task in started['tasks']]
    app.store.claim(run_id, agent_id='actual-travel-agent', expected_task_id=first,
                    agent_workflow_key='travel_expense')
    assert cli.main(['--root', str(tmp_path), 'status', '--run-id', run_id, '--view', 'batch']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['batch'] == {
        'mode': 'frozen', 'size': 2, 'task_ids': [first, second], 'total': 2,
        'queued': 1, 'inflight': 1, 'terminal': 0, 'remaining': 2, 'queue_exhausted': False,
        'summary': {'preparing': 1, 'queued': 1}, 'outside_count': 1,
        'outside_summary': {'queued': 1},
    }
    assert status['next_candidate'] == {
        'id': second, 'request_id': '90002', 'workflow_key': 'business_entertainment',
        'received_at': '2026-10-22 10:01:00', 'status': 'queued',
    }
    app.store.claim(run_id, agent_id='actual-entertainment-agent', expected_task_id=second,
                    agent_workflow_key='business_entertainment')
    assert cli.main(['--root', str(tmp_path), 'status', '--view', 'batch']) == 0
    inflight = json.loads(capsys.readouterr().out)
    assert inflight['next_candidate'] is None
    assert inflight['batch']['inflight'] == 2
    assert inflight['batch']['queue_exhausted'] is False
    app.fail(run_id, first, 'read_failed')
    app.fail(run_id, second, 'read_failed')
    app.close(run_id)
    app.store.connection.close()
    files = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert cli.main(['--root', str(tmp_path), 'status', '--view', 'batch']) == 0
    closed = json.loads(capsys.readouterr().out)
    assert closed['batch']['remaining'] == 0
    assert closed['batch']['queue_exhausted'] is True
    assert closed['batch']['outside_count'] == 1
    assert closed['batch']['terminal'] == 2
    assert closed['next_candidate'] is None
    assert closed['owner'] == {'reachable': False}
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob('*')
            if path.is_file() and not path.name.endswith(('-wal', '-shm'))} == files
    assert cli.main(['--root', str(tmp_path), 'status']) == 0
    default = json.loads(capsys.readouterr().out)
    assert default['summary'] == {'failed': 2, 'queued': 1}
    assert [task['id'] for task in default['tasks']] == [first, second, outside]
    assert set(default) == {'status', 'run', 'summary', 'tasks', 'owner'}


def test_batch_status_reports_actual_agent_generation_and_fifo_candidate(tmp_path, capsys):
    app = create_demo(tmp_path)
    template = app.api.rows[0]
    app.api.rows = [{**template, 'request_id': str(value)} for value in range(108, 100, -1)]
    started = app.pending(batch_size=7)
    run_id = started['run_id']
    task_ids = [task['id'] for task in started['tasks']]
    for task_id in task_ids[:5]:
        assert app.store.claim(run_id, agent_id='actual-native-generation-1', expected_task_id=task_id,
                               agent_workflow_key='travel_expense')['status'] == 'claimed'
        app.fail(run_id, task_id, 'read_failed')
    app.store.claim(run_id, agent_id='actual-native-generation-2', expected_task_id=task_ids[5],
                    agent_workflow_key='travel_expense')
    assert cli.main(['--root', str(tmp_path), 'status', '--view', 'batch']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['agents'] == [
        {'agent_id': 'actual-native-generation-1', 'workflow_keys': ['travel_expense'],
         'claimed_count': 5, 'inflight_task_ids': [], 'busy': False, 'claim_limit_reached': True},
        {'agent_id': 'actual-native-generation-2', 'workflow_keys': ['travel_expense'],
         'claimed_count': 1, 'inflight_task_ids': [task_ids[5]], 'busy': True, 'claim_limit_reached': False},
    ]
    assert status['capacity'] == {'inflight': 1, 'available': 2, 'limit': 3}
    assert status['next_candidate']['request_id'] == '107'
    assert status['batch']['outside_count'] == 1
    assert app.store.claim(run_id, agent_id='actual-native-generation-1', expected_task_id=task_ids[6],
                           agent_workflow_key='travel_expense')['reason'] == 'agent_claim_limit'
    assert app.store.claim(run_id, agent_id='actual-native-generation-2', expected_task_id=task_ids[6],
                           agent_workflow_key='travel_expense')['reason'] == 'agent_busy'


def record_submission(store, task_id, *, status=None, result=None, opinion='合成意见'):
    store.freeze(task_id, {}, 'synthetic-material.json', 'synthetic-digest', {}, 'synthetic-rule.md')
    review = store.review(task_id, opinion)
    submission_id = store.begin_submission(task_id, review['id'])
    if status is not None:
        store.finish_submission(submission_id, status, result or {})
    return submission_id


@pytest.mark.parametrize('handled', [True, False])
@pytest.mark.parametrize('stale_socket', [False, True])
def test_postcheck_reconciles_closed_owner_without_resubmitting(tmp_path, monkeypatch, capsys, request, handled, stale_socket):
    import socket

    from apps.oa.approvals import owner
    from apps.oa.approvals.client import socket_path
    from apps.oa.approvals.operations import Approval

    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path / 'project')
    monkeypatch.setattr(owner, 'require_production_main', lambda: None)
    app = create_demo(tmp_path / 'round')
    started = app.pending()
    task = started['tasks'][0]
    record_submission(app.store, task['id'], status='unknown')
    submission = app.store.details(task['id'])['submissions'][0]
    app.close(started['run_id'])
    if stale_socket:
        path = socket_path(app.root)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.bind(str(path))
        request.addfinalizer(lambda: path.unlink(missing_ok=True))
    if handled:
        app.api.rows = [row for row in app.api.rows if row['request_id'] != task['request_id']]
        app.api.handled = [{'request_id': task['request_id'], 'handled_at': submission['started_at']}]
    reads = []

    def api(_self):
        reads.append('read')
        return app.api

    def submit(*args, **kwargs):
        pytest.fail('postcheck must not send a submission')

    monkeypatch.setattr(Approval, '_api', api)
    monkeypatch.setattr(app.api, 'submit_and_verify', submit)
    assert cli.main(['--root', str(app.root), 'postcheck', '--task-id', str(task['id'])]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == ('submitted' if handled else 'unknown')
    assert result['submit_ok'] is handled
    assert result['done_hit'] is handled
    assert result['postcheck_done_hit'] is handled
    assert app.store.run(started['run_id'])['status'] == ('closed' if handled else 'blocked')
    assert app.store.run(started['run_id'])['ended_at'] is not None
    assert app.store.latest_run()['id'] == started['run_id']
    assert len(app.store.details(task['id'])['submissions']) == 1
    if handled:
        reads.clear()
        assert cli.main(['--root', str(app.root), 'postcheck', '--task-id', str(task['id'])]) == 0
        assert json.loads(capsys.readouterr().out)['status'] == 'submitted'
        assert not reads
    app.store.connection.close()


@pytest.mark.parametrize('locked', ['round', 'shared'])
def test_postcheck_without_owner_respects_existing_locks(tmp_path, monkeypatch, capsys, locked):
    from apps.oa.approvals import owner
    from apps.oa.approvals.operations import Approval

    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path / 'project')
    app = create_demo(tmp_path / 'round')
    started = app.pending()
    task_id = started['tasks'][0]['id']
    record_submission(app.store, task_id, status='unknown')
    app.close(started['run_id'])
    before = app.store.details(task_id)
    monkeypatch.setattr(Approval, '_api', lambda self: pytest.fail('locked postcheck must not read OA'))
    directory = app.root if locked == 'round' else owner.PROJECT_ROOT / 'runtime'
    with owner.round_lock(directory):
        assert cli.main(['--root', str(app.root), 'postcheck', '--task-id', str(task_id)]) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'BlockingIOError'
    assert app.store.details(task_id) == before
    app.store.connection.close()


def test_postcheck_without_owner_preserves_unknown_when_read_fails(tmp_path, monkeypatch, capsys):
    from apps.oa.approvals import owner
    from apps.oa.approvals.operations import Approval

    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path / 'project')
    monkeypatch.setattr(owner, 'require_production_main', lambda: None)
    app = create_demo(tmp_path / 'round')
    started = app.pending()
    task_id = started['tasks'][0]['id']
    record_submission(app.store, task_id, status='unknown')
    app.close(started['run_id'])
    before = app.store.details(task_id)

    def unavailable(_self):
        raise RuntimeError('controlled_read_failure')

    monkeypatch.setattr(Approval, '_api', unavailable)
    assert cli.main(['--root', str(app.root), 'postcheck', '--task-id', str(task_id)]) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'controlled_read_failure'
    assert app.store.details(task_id) == before
    app.store.connection.close()


def test_postcheck_without_owner_requires_published_main_before_reading(tmp_path, monkeypatch, capsys):
    from apps.oa.approvals import owner
    from apps.oa.approvals.operations import Approval

    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path / 'project')
    app = create_demo(tmp_path / 'round')
    task_id = app.pending()['tasks'][0]['id']
    record_submission(app.store, task_id, status='unknown')
    before = app.store.details(task_id)

    def refuse_main():
        raise ValueError('production_main_not_synced')

    monkeypatch.setattr(owner, 'require_production_main', refuse_main)
    monkeypatch.setattr(Approval, '_api', lambda self: pytest.fail('unpublished code must not read OA'))
    assert cli.main(['--root', str(app.root), 'postcheck', '--task-id', str(task_id)]) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'production_main_not_synced'
    assert app.store.details(task_id) == before
    app.store.connection.close()


def test_batch_status_acceptance_uses_scoped_receipts_and_exposes_unresolved_and_duplicate_attempts(tmp_path, capsys):
    app = create_demo(tmp_path)
    template = app.api.rows[0]
    app.api.rows = [{**template, 'request_id': str(value)} for value in range(101, 105)]
    old = app.pending(batch_size=4)
    first_id = old['tasks'][0]['id']
    verified = {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}
    old_attempt = record_submission(app.store, first_id, status='submitted', result=verified)
    app.store.set_status(first_id, 'reviewed')
    app.close(old['run_id'])
    current = app.pending(batch_size=3)
    run_id = current['run_id']
    first, second, third, outside = [task['id'] for task in current['tasks']]
    submitted = record_submission(app.store, first, status='submitted', result=verified)
    app.store.set_status(first, 'reviewed')
    duplicate = record_submission(app.store, first, status='submitted', result=verified)
    unknown = record_submission(app.store, second, status='unknown', result={
        'submit_ok': True, 'done_hit': False, 'postcheck_done_hit': False,
        'failure_code': 'writer_postcheck_timeout'})
    unfinished = record_submission(app.store, third)
    record_submission(app.store, outside, status='submitted', result=verified)
    app.close(run_id)
    app.store.connection.close()
    assert cli.main(['--root', str(tmp_path), 'status', '--run-id', run_id, '--view', 'batch']) == 0
    acceptance = json.loads(capsys.readouterr().out)['acceptance']
    assert {key: value for key, value in acceptance.items() if key != 'tasks'} == {
        'task_count': 3, 'attempt_count': 4, 'verified_attempt_count': 2,
        'unknown_attempt_count': 1, 'unfinished_attempt_count': 1, 'unverified_attempt_count': 2,
        'duplicate_attempt_count': 1, 'tasks_with_verified_submission': 1,
        'tasks_without_verified_submission': 2, 'all_verified': False,
    }
    first_facts, second_facts, third_facts = acceptance['tasks']
    assert [item['task_id'] for item in acceptance['tasks']] == [first, second, third]
    assert [item['id'] for item in first_facts['submissions']] == [submitted, duplicate]
    assert old_attempt not in [item['id'] for item in first_facts['submissions']]
    assert first_facts['verified_attempt_count'] == 2
    assert first_facts['duplicate_attempt_count'] == 1
    assert first_facts['submissions'][0]['review_id'] == 1
    assert first_facts['submissions'][0]['verified'] is True
    assert second_facts['submissions'][0]['id'] == unknown
    assert second_facts['submissions'][0]['submit_ok'] is True
    assert second_facts['submissions'][0]['postcheck_done_hit'] is False
    assert second_facts['submissions'][0]['failure_code'] == 'writer_postcheck_timeout'
    assert second_facts['submissions'][0]['verified'] is False
    assert second_facts['unknown_attempt_count'] == 1
    assert third_facts['submissions'][0]['id'] == unfinished
    assert third_facts['submissions'][0]['completed_at'] is None
    assert third_facts['submissions'][0]['postcheck_done_hit'] is None
    assert third_facts['unfinished_attempt_count'] == 1
    assert cli.main(['--root', str(tmp_path), 'status', '--run-id', old['run_id'], '--view', 'batch']) == 0
    old_acceptance = json.loads(capsys.readouterr().out)['acceptance']
    assert old_acceptance['attempt_count'] == 1
    assert old_acceptance['tasks'][0]['submissions'][0]['id'] == old_attempt


def test_unbounded_status_requires_verification_facts_even_when_tasks_are_submitted(tmp_path, capsys):
    app = create_demo(tmp_path)
    started = app.pending()
    first, second, third = [task['id'] for task in started['tasks']]
    verified = {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}
    record_submission(app.store, first, status='submitted', result=verified, opinion='请退回补充材料。')
    record_submission(app.store, second, status='submitted', result={'submit_ok': True})
    record_submission(app.store, third, status='submitted', result={**verified, 'fields_match': False})
    assert cli.main(['--root', str(tmp_path), 'status', '--view', 'batch']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['batch']['mode'] == 'all_queue'
    assert status['batch']['size'] is None
    assert status['batch']['outside_count'] == 0
    assert status['batch']['queue_exhausted'] is True
    assert status['next_candidate'] is None
    assert status['acceptance']['verified_attempt_count'] == 1
    assert status['acceptance']['unverified_attempt_count'] == 2
    assert status['acceptance']['all_verified'] is False
    record = status['acceptance']['tasks'][0]
    assert record['verified_attempt_count'] == 1
    assert 'business_approved' not in record


def test_batch_status_does_not_create_a_missing_root(tmp_path, capsys):
    root = tmp_path / 'missing'
    assert cli.main(['--root', str(root), 'status', '--view', 'batch']) == 0
    assert json.loads(capsys.readouterr().out) == {'status': 'not_initialized'}
    assert not root.exists()


def test_batch_queue_exhaustion_keeps_unknown_submission_blocked(tmp_path, capsys):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    record_submission(app.store, started['tasks'][0]['id'], status='unknown', result={
        'submit_ok': True, 'done_hit': False, 'postcheck_done_hit': False})
    assert cli.main(['--root', str(tmp_path), 'status', '--view', 'batch']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['batch']['queue_exhausted'] is True
    assert 'complete' not in status['batch']
    assert status['batch']['summary'] == {'unknown': 1}
    assert status['run']['status'] == 'blocked'
    assert status['acceptance']['unknown_attempt_count'] == 1
    assert status['acceptance']['all_verified'] is False


def test_overseas_acceptance_requires_explicit_field_verification(tmp_path, capsys):
    app = create_demo(tmp_path)
    run_id = app.store.start_run([
        {'request_id': str(value), 'received_at': '2026-10-22 10:00:00', 'workflow_key': 'overseas_invoice'}
        for value in range(101, 104)], batch_size=3)
    verified = {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}
    for task, fields in zip(app.store.tasks(run_id), [{}, {'fields_match': False}, {'fields_match': True}]):
        record_submission(app.store, task['id'], status='submitted', result={**verified, **fields})
    assert cli.main(['--root', str(tmp_path), 'status', '--run-id', run_id, '--view', 'batch']) == 0
    acceptance = json.loads(capsys.readouterr().out)['acceptance']
    assert acceptance['verified_attempt_count'] == 1
    assert acceptance['unverified_attempt_count'] == 2
    assert acceptance['all_verified'] is False
    receipts = [task['submissions'][0] for task in acceptance['tasks']]
    assert [item['fields_match'] for item in receipts] == [None, False, True]
    assert [item['verified'] for item in receipts] == [False, False, True]


@pytest.mark.parametrize(('workflow_key', 'fields', 'stored_status', 'completed_at', 'expected_status', 'verified'), [
    ('general_expense', {}, 'submitted', 'finished', 'submitted', True),
    ('overseas_invoice', {}, 'submitted', 'finished', 'submitted', False),
    ('overseas_invoice', {'fields_match': True}, 'submitted', 'finished', 'submitted', True),
    ('general_expense', {'fields_match': False}, 'submitted', 'finished', 'submitted', False),
    ('general_expense', {'done_hit': 1}, 'submitted', 'finished', 'unknown', False),
    ('general_expense', {}, 'unknown', 'finished', 'submitted', False),
    ('general_expense', {}, 'submitted', None, 'submitted', False),
])
def test_receipt_assessment_preserves_legacy_facts_and_separates_task_status(
        workflow_key, fields, stored_status, completed_at, expected_status, verified):
    from apps.oa.approvals.receipts import assess_submission, submission_status

    receipt = {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True,
               'legacy_evidence': {'unrecognized': ['kept']}, **fields}
    submission = {'status': stored_status, 'completed_at': completed_at, 'result_json': json.dumps(receipt)}
    original = submission.copy()

    evidence = assess_submission(submission, workflow_key=workflow_key)

    assert evidence.receipt == receipt
    assert ('fields_match' in evidence.receipt) is ('fields_match' in fields)
    assert evidence.verified is verified
    assert submission_status(evidence.receipt) == expected_status
    assert submission == original

def test_orphaned_finish_closes_killed_owner_without_changing_task_facts(tmp_path, monkeypatch, capsys):
    from apps.oa.approvals import owner
    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path / 'project')
    process = subprocess.Popen([sys.executable, '-m', 'apps.oa.approvals', '--root', str(tmp_path), 'start', '--demo'],
                               cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = json.loads(process.stdout.readline())
    process.kill()
    process.wait(timeout=5)
    def invoke(*args):
        code = cli.main(['--root', str(tmp_path), *args])
        return code, json.loads(capsys.readouterr().out)
    before = invoke('status')[1]
    assert before['run']['status'] == 'running'
    assert before['owner']['reachable'] is False
    wrong, _ = invoke('finish', '--run-id', 'wrong', '--orphaned')
    assert wrong == 1
    finished, result = invoke('finish', '--run-id', started['run_id'], '--orphaned')
    assert finished == 0, result
    assert result['status'] == 'closed'
    after = invoke('status')[1]
    assert after['run']['status'] == 'closed'
    assert after['tasks'] == before['tasks']
    assert after['owner']['reachable'] is False


@pytest.mark.parametrize('locked', ['round', 'shared'])
def test_orphaned_finish_refuses_held_locks(tmp_path, monkeypatch, capsys, locked):
    from apps.oa.approvals import owner
    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path / 'project')
    root = tmp_path / 'round'
    app = create_demo(root)
    run_id = app.pending()['run_id']
    directory = root if locked == 'round' else owner.PROJECT_ROOT / 'runtime'
    with owner.round_lock(directory):
        assert cli.main(['--root', str(root), 'finish', '--run-id', run_id, '--orphaned']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
    assert app.store.run(run_id)['status'] == 'running'
    app.store.connection.close()


@pytest.mark.parametrize('state', ['preparing', 'started', 'unknown', 'unverified'])
def test_orphaned_finish_preserves_unresolved_work(tmp_path, monkeypatch, capsys, state):
    from apps.oa.approvals import owner
    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path / 'project')
    app = create_demo(tmp_path)
    started = app.pending()
    run_id, task_id = started['run_id'], started['tasks'][0]['id']
    if state == 'preparing':
        app.store.claim(run_id, agent_id='reviewer', expected_task_id=task_id, agent_workflow_key='travel_expense')
    else:
        record_submission(app.store, task_id, status=None if state == 'started' else 'unknown' if state == 'unknown' else 'submitted')
    before = app.store.details(task_id)
    before_run = app.store.run(run_id)
    assert cli.main(['--root', str(tmp_path), 'finish', '--run-id', run_id, '--orphaned']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['error'] == ('collection_unresolved' if state == 'preparing' else 'submission_unresolved')
    assert app.store.run(run_id) == before_run
    assert app.store.details(task_id) == before
    app.store.connection.close()


def test_sigterm_closes_owner_and_releases_transport(tmp_path):
    process = subprocess.Popen([sys.executable, '-m', 'apps.oa.approvals', '--root', str(tmp_path), 'start', '--demo'],
                               cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = json.loads(process.stdout.readline())
    process.terminate()
    process.wait(timeout=5)
    result = subprocess.run([sys.executable, '-m', 'apps.oa.approvals', '--root', str(tmp_path), 'status'],
                            cwd=PROJECT, capture_output=True, text=True, timeout=5)
    assert json.loads(result.stdout)['run']['status'] == 'closed'
    assert not Path(started['socket']).exists()


@pytest.mark.parametrize('signum', ['SIGTERM', 'SIGHUP'])
def test_termination_during_submit_saves_receipt_before_closing(tmp_path, monkeypatch, capsys, signum):
    import io
    import os
    import signal
    from apps.oa.approvals import demo
    original = demo.DemoApi.submit_and_verify
    submitted = []

    def submit(api, request_id, opinion):
        submitted.append(request_id)
        result = original(api, request_id, opinion)
        os.kill(os.getpid(), getattr(signal, signum))
        return result

    monkeypatch.setattr(demo.DemoApi, 'submit_and_verify', submit)
    requests = [
        {'operation': 'collect', 'agent_id': 'reviewer', 'expected_task_id': 1, 'agent_workflow_key': 'travel_expense'},
        {'operation': 'submit', 'task_id': 1, 'opinion': '合成意见', 'confirm_write': True},
        {'operation': 'collect', 'agent_id': 'second', 'expected_task_id': 2, 'agent_workflow_key': 'business_entertainment'},
    ]
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''.join(json.dumps(item) + '\n' for item in requests)))
    assert cli.main(['--root', str(tmp_path), 'start', '--demo']) == 130
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert messages[-1]['status'] == 'submitted'
    assert submitted == ['90001']
    assert cli.main(['--root', str(tmp_path), 'status', '--view', 'batch']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['run']['status'] == 'closed'
    assert status['acceptance']['verified_attempt_count'] == 1
    assert status['acceptance']['unfinished_attempt_count'] == 0
    assert status['summary'] == {'submitted': 1, 'queued': 2}
