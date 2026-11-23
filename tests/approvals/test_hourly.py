from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

import pytest

from apps.oa.approvals import cli, owner
from apps.oa.approvals.demo import create_demo
from tests.approvals.review_fixtures import collect_for_review, completed_review, deliver_and_submit, deliver_cli, review_cli
from tests.approvals.test_capability_cli import record_submission


PROJECT = Path(__file__).resolve().parents[2]


def command(root, *args):
    return [sys.executable, '-m', 'apps.oa', 'approvals', '--root', str(root), *args]


def invoke(root, *args, exit_code=0):
    result = subprocess.run(command(root, *args), cwd=PROJECT, capture_output=True, text=True, timeout=20)
    assert result.returncode == exit_code, result.stdout + result.stderr
    return json.loads(result.stdout)


def launch(root, *args):
    return subprocess.Popen(command(root, 'start', '--hourly', '--demo', '--batch-size', '40', *args),
                            cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def ready(process):
    assert select.select([process.stdout], [], [], 15)[0]
    line = process.stdout.readline()
    assert line, process.stderr.read()
    result = json.loads(line)
    assert result['status'] == 'ready', result
    return result


def stopped_run_file(root, run_id, basis='execution_stalled'):
    path = root / 'stopped-run.json'
    path.write_text(json.dumps({'run_id': run_id, 'basis': basis, 'evidence': 'Synthetic tool terminal and exit evidence'}))
    return path


def test_hourly_refreshes_lark_roster_for_each_run(tmp_path, monkeypatch, capsys):
    from apps.oa.approvals.sources import oa
    from apps.oa.approvals.sources import travel_reconciliation

    root = tmp_path / 'runtime' / 'hourly-preaudit'
    app = create_demo(tmp_path / 'fake-oa')
    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(owner, 'require_production_main', lambda: None)
    monkeypatch.setattr(owner, 'load_source_policy', lambda: app.source_policy)
    monkeypatch.setattr(oa, 'build_api', lambda *args, **kwargs: app.api)
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'private-auth.env'))
    config = {'spreadsheet_token': 'syntheticWorkbook', 'sheet_id': 'sheetA', 'identity': 'user'}
    (tmp_path / 'travel-reconciliation-sheet.json').write_text(json.dumps(config))
    response_file = tmp_path / 'lark-response.json'
    calls = tmp_path / 'lark-calls.txt'
    pending_rows = app.api.pending_rows

    def pending():
        with calls.open('a') as log:
            log.write('queue\n')
        return pending_rows()

    monkeypatch.setattr(app.api, 'pending_rows', pending)
    executable = tmp_path / 'lark-cli'
    executable.write_text(
        f'#!{sys.executable}\n'
        'import os, sys\n'
        'from pathlib import Path\n'
        "assert not {'OA_APP_SECRET', 'OA_DB_PASSWORD', 'OA_AUTH_ENV_FILE'} & os.environ.keys()\n"
        "assert sys.argv[1:] == ['sheets', '+table-get', '--spreadsheet-token', 'syntheticWorkbook', "
        "'--sheet-id', 'sheetA', '--as', 'user', '--max-chars', '250000']\n"
        "root = Path(__file__).parent\n"
        "with (root / 'lark-calls.txt').open('a') as calls: calls.write('read\\n')\n"
        "print((root / 'lark-response.json').read_text())\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', os.pathsep.join((str(tmp_path), os.environ['PATH'])))
    monkeypatch.setenv('OA_APP_SECRET', 'synthetic-oa-credential')
    monkeypatch.setenv('OA_DB_PASSWORD', 'synthetic-db-credential')

    def run(rows, *, truncated=False):
        sheet = {'name': 'Synthetic', 'range': 'A1:B3', 'columns': ['工号', '状态'], 'data': rows}
        response_file.write_text(json.dumps({'ok': True, 'identity': 'user',
            'data': {'sheets': [sheet], 'truncated': truncated}}))
        monkeypatch.setattr(sys, 'stdin', io.StringIO('{"operation":"finish"}\n'))
        assert cli.main(['--root', str(root), 'start', '--hourly', '--workflow', 'travel_expense']) == 0
        ready_line = capsys.readouterr().out.splitlines()[0]
        assert 'synthetic-id' not in ready_line
        started = json.loads(ready_line)
        assert started['status'] == 'ready'
        assert invoke(root, 'status', '--view', 'batch')['travel_reconciliation'] == started['travel_reconciliation']
        return started['travel_reconciliation']

    try:
        original = run([['synthetic-id-a', '暂停']])
        original_file = Path(original['snapshot_file'])
        current = run([['synthetic-id-a', '已解除'], ['synthetic-id-b', '暂停']])
        current_file = Path(current['snapshot_file'])
        assert original_file != current_file
        assert original['employee_rows'] == 1
        assert current['employee_rows'] == 2
        assert json.loads(original_file.read_text())['table']['data'] == [['synthetic-id-a', '暂停']]
        assert json.loads(current_file.read_text())['table']['data'] == [
            ['synthetic-id-a', '已解除'], ['synthetic-id-b', '暂停']]
        assert run([], truncated=True) == {'status': 'failed', 'error': 'travel_reconciliation_result_invalid'}
        with monkeypatch.context() as failure:
            def unwritable(*args, **kwargs):
                raise PermissionError

            failure.setattr(travel_reconciliation, 'write_bytes', unwritable)
            assert run([['synthetic-id-b', '暂停']]) == {
                'status': 'failed', 'error': 'travel_reconciliation_snapshot_write_failed'}
        assert calls.read_text().splitlines() == ['read', 'queue'] * 4
    finally:
        app.store.connection.close()


def test_hourly_keeps_progressing_batch_and_restores_stalled_batch_in_same_start(tmp_path):
    with launch(tmp_path) as original:
        try:
            started = ready(original)
            task = started['tasks'][0]
            collected = review_cli(tmp_path, lambda *args: invoke(tmp_path, *args),
                                   str(task['id']), 'reviewer', task['workflow_key'])
            material = Path(collected['material_file'])
            previous_bytes = material.read_bytes()
            state = invoke(tmp_path, 'status', '--view', 'batch')
            assert state['hourly_dispatch']['action'] == 'inspect_owner'
            assert state['owner']['run_id'] == started['run_id']
            assert state['batch']['inflight'] == 1

            proof = stopped_run_file(tmp_path, started['run_id'])
            start_args = ('start', '--hourly', '--demo', '--stopped-run-file', str(proof))
            assert invoke(tmp_path, *start_args)['status'] == 'already_running'
            next_task = started['tasks'][1]
            assert invoke(tmp_path, 'collect', '--expected-task-id', str(next_task['id']))['status'] == 'prepared'

            os.kill(original.pid, signal.SIGSTOP)
            assert invoke(tmp_path, *start_args)['status'] == 'already_running'
            original.kill()
            original.wait(timeout=10)
        finally:
            if original.poll() is None:
                original.kill()
                original.wait(timeout=10)

    lost = invoke(tmp_path, 'status', '--view', 'batch')
    assert lost['hourly_dispatch']['action'] == 'confirm_stopped'
    refused = invoke(tmp_path, 'start', '--demo', '--hourly', exit_code=1)
    assert refused['error'] == 'stopped_run_evidence_required'
    with launch(tmp_path, '--stopped-run-file', str(proof)) as replacement:
        try:
            new = ready(replacement)
            assert new['recovered_run']['run_id'] == started['run_id']
            assert new['recovered_run']['closure']['kind'] == 'hourly_recovery'
            assert new['run_id'] != started['run_id']
            assert new['tasks'][0]['status'] == 'queued'
            prepared = invoke(tmp_path, 'collect', '--expected-task-id', str(task['id']))
            assert prepared['status'] == 'prepared'
            assert Path(prepared['material_file']) != material and material.read_bytes() == previous_bytes
            old = invoke(tmp_path, 'status', '--view', 'batch', '--run-id', started['run_id'])
            assert old['closed_batch']['summary'] == {'reviewing': 1, 'prepared': 1, 'queued': 1}
            assert old['execution']['unfinished_count'] == 2
            opinion = tmp_path / 'synthetic-opinion.txt'
            opinion.write_text('Synthetic recovered review')
            receipt = deliver_cli(tmp_path, lambda *args: invoke(tmp_path, *args),
                                  str(task['id']), str(opinion), confirm_write=True)
            assert receipt['status'] == 'submitted' and receipt['postcheck_done_hit'] is True
            assert material.read_bytes() == previous_bytes
            assert not Path(prepared['material_file']).exists()
            invoke(tmp_path, 'finish', '--run-id', new['run_id'], '--require-drained')
            assert replacement.wait(timeout=10) == 0
        finally:
            if replacement.poll() is None:
                replacement.kill()
                replacement.wait(timeout=10)


def test_hourly_unknown_uses_postcheck_without_pending_start_or_resubmit(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'round'
    app = create_demo(root)
    original = app.pending()
    task = original['tasks'][0]
    submission = record_submission(app.store, task['id'], status='unknown')
    proof = stopped_run_file(root, original['run_id'], 'owner_lost')

    def no_pending(*args, **kwargs):
        pytest.fail('hourly start must not fetch OA pending while a submission is unknown')

    def run(*args, code=0):
        assert cli.main(['--root', str(root), *args]) == code
        return json.loads(capsys.readouterr().out)

    monkeypatch.setattr(type(app), 'pending', no_pending)
    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(owner, 'require_production_main', lambda: None)
    monkeypatch.setattr(owner, 'load_source_policy', lambda: app.source_policy)
    assert run('status', '--view', 'batch')['hourly_dispatch']['action'] == 'postcheck'
    assert run('start', '--hourly', '--demo', '--stopped-run-file', str(proof),
               code=1)['error'] == 'submission_unresolved'
    assert app.store.latest_run()['id'] == original['run_id']

    from apps.oa.approvals.sources import oa

    monkeypatch.setattr(oa, 'build_api', lambda *args, **kwargs: app.api)
    assert run('postcheck', '--task-id', str(task['id']))['status'] == 'unknown'
    attempts = app.store.details(task['id'])['submissions']
    assert [attempt['id'] for attempt in attempts] == [submission]
    assert app.api.handled == []
    app.store.connection.close()


def test_hourly_reports_isolated_delivery_in_each_round(tmp_path):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    task = started['tasks'][0]
    collect_for_review(app, started['run_id'], expected_task_id=task['id'],
                       agent_id='first', agent_workflow_key=task['workflow_key'])
    app.fail(started['run_id'], task['id'], 'synthetic_attachment_unreadable')

    def reported():
        report = invoke(tmp_path, 'status', '--view', 'batch')['isolated_failures']
        assert report['pending_snapshot_at']
        assert len(report['items']) == 1
        failure = report['items'][0]
        assert failure['request_id'] == task['request_id']
        assert failure['workflow_key'] == task['workflow_key']
        assert failure['reason'] == 'synthetic_attachment_unreadable'
        assert failure['suggested_action']
        return report['new_count']

    assert reported() == 1
    prior = app.close(started['run_id'], handoff=True)
    after = app.pending(continue_from=prior['run_id'])
    assert reported() == 0
    app.close(after['run_id'])
    later = app.pending()
    assert reported() == 0
    app.close(later['run_id'])
    app.api.rows = [row for row in app.api.rows if row['request_id'] != task['request_id']]
    app.pending()
    assert invoke(tmp_path, 'status', '--view', 'batch')['isolated_failures']['items'] == []
    app.store.connection.close()


def test_hourly_stops_for_user_stop_and_prefers_exact_ready_continuation(tmp_path):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    task = started['tasks'][0]
    collect_for_review(app, started['run_id'], expected_task_id=task['id'],
                       agent_id='another', agent_workflow_key=task['workflow_key'])
    app.fail(started['run_id'], task['id'], 'synthetic_continuation_failure')
    closed = app.close(started['run_id'], handoff=True)
    assert closed['handoff']['status'] == 'ready'
    assert invoke(tmp_path, 'status', '--view', 'batch')['hourly_dispatch']['action'] == 'continue'
    proof = stopped_run_file(tmp_path, closed['run_id'])
    denied = invoke(tmp_path, 'start', '--hourly', '--demo', '--stopped-run-file', str(proof), exit_code=1)
    assert denied['error'] == 'continuation_pending'

    with subprocess.Popen(command(tmp_path, 'start', '--hourly', '--demo', '--continue-from', closed['run_id']),
                          cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as process:
        try:
            resumed = ready(process)
            stop = tmp_path / 'stop.json'
            stop.write_text(json.dumps({'kind': 'user_stop', 'reason': 'Synthetic stop'}))
            invoke(tmp_path, 'finish', '--run-id', resumed['run_id'], '--require-drained', '--stop-file', str(stop))
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    assert invoke(tmp_path, 'status', '--view', 'batch')['hourly_dispatch']['action'] == 'stopped'
    assert invoke(tmp_path, 'start', '--hourly', '--demo', exit_code=1)['error'] == 'user_stop'
    app.store.connection.close()


def test_hourly_resumes_from_saved_closure_after_pending_refresh_fails(tmp_path, monkeypatch, capsys):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    task = started['tasks'][0]
    collect_for_review(app, started['run_id'], expected_task_id=task['id'],
                       agent_id='legacy-reviewer', agent_workflow_key=task['workflow_key'])
    closed = app.close(started['run_id'])
    proof = stopped_run_file(tmp_path, closed['run_id'], 'orphan_close_receipt')
    latest = invoke(tmp_path, 'status', '--view', 'batch')
    assert latest['hourly_dispatch']['action'] == 'confirm_stopped'
    assert latest['closure'] is None
    assert latest['closed_batch']['summary'] == {'reviewing': 1}

    def no_pending(*args, **kwargs):
        raise ValueError('synthetic_pending_outage')

    with monkeypatch.context() as patch:
        patch.setattr(type(app), 'pending', no_pending)
        assert cli.main(['--root', str(tmp_path), 'start', '--hourly', '--demo',
                         '--stopped-run-file', str(proof)]) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'synthetic_pending_outage'
    latest = invoke(tmp_path, 'status', '--view', 'batch')
    assert latest['hourly_dispatch']['action'] == 'start'
    assert latest['closure']['basis'] == 'orphan_close_receipt'
    assert latest['run']['id'] == closed['run_id']
    assert latest['closed_batch']['summary'] == {'reviewing': 1}
    assert latest['execution']['unfinished_count'] == 1
    with launch(tmp_path) as process:
        try:
            fresh = ready(process)
            assert fresh['tasks'][0]['status'] == 'queued'
            invoke(tmp_path, 'finish', '--run-id', fresh['run_id'], '--require-drained')
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    app.store.connection.close()


def test_hourly_refuses_recovery_while_writer_still_holds_lock(tmp_path):
    app = create_demo(tmp_path)
    started = app.pending()
    proof = stopped_run_file(tmp_path, started['run_id'])
    with app._writer_lock():
        result = invoke(tmp_path, 'start', '--hourly', '--demo', '--stopped-run-file', str(proof))
    assert result['status'] == 'already_running'
    assert app.store.latest_run()['id'] == started['run_id']
    assert app.store.latest_run()['status'] == 'running'
    app.store.connection.close()


def test_stopped_round_workers_cannot_change_the_reassigned_delivery(tmp_path):
    app = create_demo(tmp_path)
    old = app.pending()
    task_id = old['tasks'][0]['id']
    assert app.store.claim(old['run_id'], expected_task_id=task_id)['status'] == 'claimed'
    app.close(old['run_id'])
    new = app.pending()
    assert app.store.claim(new['run_id'], expected_task_id=task_id)['status'] == 'claimed'

    before = app.store.details(task_id)
    frozen = tmp_path / 'synthetic-stale-material.json'
    with pytest.raises(ValueError, match='collection_run_changed'):
        app.store.freeze(old['run_id'], task_id, {}, frozen, 'synthetic-digest', {}, frozen)
    app.store.fail_collection(old['run_id'], task_id, 'synthetic_late_failure')
    with pytest.raises(ValueError, match='task_run_mismatch'):
        owner.dispatch(app, old['run_id'], {'operation': 'submit', 'task_id': task_id, 'confirm_write': True})
    assert app.store.details(task_id) == before
    app.store.connection.close()


def test_hourly_report_links_collection_failure_saved_before_freeze(tmp_path):
    app = create_demo(tmp_path)
    original = app.pending(batch_size=1)
    task_id = original['tasks'][0]['id']

    def fail_collection(*args, **kwargs):
        raise ValueError('synthetic_source_unavailable')

    app.collector = fail_collection
    with pytest.raises(ValueError, match='synthetic_source_unavailable') as error:
        app.collect(original['run_id'], expected_task_id=task_id)
    failure, = invoke(tmp_path, 'status', '--view', 'batch')['isolated_failures']['items']
    assert failure['task_id'] == task_id
    assert failure['stage'] == 'collection'
    assert failure['reason'].endswith('synthetic_source_unavailable')
    assert failure['evidence_file'] == error.value.diagnostics_file
    assert Path(failure['evidence_file']).is_file()
    app.store.connection.close()


def test_hourly_report_tolerates_expired_processed_task_in_pending_snapshot(tmp_path):
    app = create_demo(tmp_path)
    pending = app.api.pending_rows()
    original = app.pending(batch_size=1)
    task = original['tasks'][0]
    collect_for_review(app, original['run_id'], expected_task_id=task['id'],
                       agent_id='expiry-reviewer', agent_workflow_key=task['workflow_key'])
    result = deliver_and_submit(app, task['id'], opinion='Synthetic receipt', confirm_write=True,
                                review_assessment=completed_review(app, task['id']))
    assert result['status'] == 'submitted'
    assert app.close(original['run_id'], handoff=True)['handoff']['status'] == 'ready'
    app.api.rows = pending
    app.pending(continue_from=original['run_id'])
    assert app.store.expire_history(datetime.now(timezone.utc) + timedelta(days=1)) == 1

    report = invoke(tmp_path, 'status', '--view', 'batch')
    assert report['pending']['excluded'][0]['task_id'] == task['id']
    assert report['isolated_failures']['items'] == []
    app.store.connection.close()


@pytest.mark.parametrize('already_recovering', [False, True])
def test_hourly_preserves_partial_preparation_and_existing_recovery_attempt(tmp_path, already_recovering):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    task = started['tasks'][0]
    if already_recovering:
        collect_for_review(app, started['run_id'], expected_task_id=task['id'],
                           agent_id='first-reviewer', agent_workflow_key=task['workflow_key'])
        app.fail(started['run_id'], task['id'], 'synthetic_first_failure')
        app.close(started['run_id'])
        started = app.pending(recovery_request_ids=[task['request_id']], batch_size=1)

    assert app.store.claim(started['run_id'], expected_task_id=task['id'])['status'] == 'claimed'
    partial = tmp_path / 'materials' / str(task['id']) / 'interrupted-attempt' / 'partial.txt'
    partial.parent.mkdir(parents=True)
    partial.write_text('Synthetic partial download')
    proof = stopped_run_file(tmp_path, started['run_id'], 'owner_lost')
    with launch(tmp_path, '--stopped-run-file', str(proof)) as process:
        try:
            recovered = ready(process)
            assert recovered['recovered_run']['run_id'] == started['run_id']
            assert invoke(tmp_path, 'collect', '--expected-task-id', str(task['id']))['status'] == 'prepared'
            if already_recovering:
                invoke(tmp_path, 'fail', '--task-id', str(task['id']), '--reason', 'synthetic_repeat_failure')
                state = invoke(tmp_path, 'status', '--view', 'batch')
                assert state['recovery_candidates'] == []
                assert state['isolated_failures']['items'][0]['reason'] == 'synthetic_repeat_failure'
            else:
                opinion = tmp_path / 'retry-opinion.txt'
                opinion.write_text('Synthetic review after partial preparation')
                result = deliver_cli(tmp_path, lambda *args: invoke(tmp_path, *args),
                                     str(task['id']), str(opinion), confirm_write=True)
                assert result['status'] == 'submitted'
            assert partial.read_text() == 'Synthetic partial download'
            invoke(tmp_path, 'finish', '--run-id', recovered['run_id'], '--require-drained')
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    app.store.connection.close()


def test_hourly_stale_verified_delivery_does_not_count_or_block_fresh_work(tmp_path):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    task = started['tasks'][0]
    submission = record_submission(app.store, task['id'], status='submitted', result={
        'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True})
    app.close(started['run_id'])
    stale = app.pending(batch_size=1, request_ids=[task['request_id']])
    app.close(stale['run_id'])

    status = invoke(tmp_path, 'status', '--view', 'batch')
    assert status['execution']['verified_count'] == 0
    assert status['runtime_unresolved'] == []
    assert status['hourly_dispatch']['action'] == 'start'
    assert 'submission_unverified' not in status['closeout']['blockers']
    assert [attempt['id'] for attempt in app.store.details(task['id'])['submissions']] == [submission]
    no_progress = app.pending(batch_size=1)
    assert app.close(no_progress['run_id'], handoff=True)['handoff']['reason'] == 'continuation_no_progress'
    app.store.connection.close()


def test_hourly_requires_reconciliation_for_missing_receipt_without_queryable_attempt(tmp_path):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    app.store.set_status(started['tasks'][0]['id'], 'submitted')
    app.close(started['run_id'])

    status = invoke(tmp_path, 'status', '--view', 'batch')
    assert status['hourly_dispatch']['action'] == 'blocked'
    assert status['hourly_dispatch']['reason'] == 'submission_evidence_requires_review'
    assert invoke(tmp_path, 'start', '--hourly', '--demo', exit_code=1)['error'] == 'submission_evidence_requires_review'
    app.store.connection.close()


def test_hourly_recovers_blocked_handoff_after_unknown_is_verified(tmp_path):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    task = started['tasks'][0]
    submission = record_submission(app.store, task['id'], status='unknown')
    app.api.rows = [row for row in app.api.rows if row['request_id'] != task['request_id']]
    app.api.handled = [{'request_id': task['request_id'], 'handled_at': '2999-01-01 00:00:00'}]
    assert app.postcheck(task['id'])['status'] == 'submitted'
    closed = app.close(started['run_id'], handoff=True)
    assert closed['handoff']['reason'] == 'continuation_unresolved_tasks'
    assert invoke(tmp_path, 'status', '--view', 'batch')['hourly_dispatch']['action'] == 'confirm_stopped'

    proof = stopped_run_file(tmp_path, started['run_id'])
    with launch(tmp_path, '--stopped-run-file', str(proof)) as process:
        try:
            recovered = ready(process)
            assert recovered['recovered_run']['run_id'] == started['run_id']
            assert [task['status'] for task in recovered['tasks']] == ['submitted', 'queued', 'queued']
            invoke(tmp_path, 'finish', '--run-id', recovered['run_id'], '--require-drained')
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    assert [attempt['id'] for attempt in app.store.details(task['id'])['submissions']] == [submission]
    app.store.connection.close()
