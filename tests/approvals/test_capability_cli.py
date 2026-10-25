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
