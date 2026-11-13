import json
from pathlib import Path

import pytest

from apps.oa.approvals import retention
from apps.oa.approvals.client import query
from tests.approvals.test_operations import service


def assessment(app, task_id=1, status='complete'):
    task = app.store.get_task(task_id)
    return {'task_id': task_id, 'material_sha256': task['material_sha256'],
            'status': status, 'reason': 'Synthetic review completed' if status == 'complete' else 'Synthetic unreadable last digit',
            'rule_basis': 'Synthetic rule permits preserving an unknown value',
            'evidence_files': [task['material_file']]}


def test_business_pending_keeps_evidence_and_continues_fifo(tmp_path):
    app, api = service(tmp_path)
    api.rows += [{**api.rows[0], 'request_id': str(i)} for i in [102, 103, 104]]
    run = app.pending(batch_size=3)['run_id']
    claimed = app.collect(run, agent_id='a', expected_task_id=1, agent_workflow_key='travel_expense')
    evidence = Path(claimed['material_file']).parent / 'reread.json'
    evidence.write_text('{"synthetic_conflict":true}')
    review = assessment(app, status='business_pending')
    review['evidence_files'].append(str(evidence))
    result = app.submit(1, opinion='Synthetic unknown value', review_assessment=review, confirm_write=True)
    assert result['submit_ok'] and result['done_hit'] and result['postcheck_done_hit']
    assert Path(claimed['material_file']).is_file() and evidence.is_file()
    assert app.collect(run, agent_id='a', expected_task_id=2, agent_workflow_key='travel_expense')['task_id'] == 2
    app.submit(2, opinion='Synthetic completed', review_assessment=assessment(app, 2), confirm_write=True)
    app.collect(run, agent_id='a', expected_task_id=3, agent_workflow_key='travel_expense')
    app.submit(3, opinion='Synthetic completed', review_assessment=assessment(app, 3), confirm_write=True)
    app.close(run, handoff=True)
    status = query(tmp_path, 'status', run_id=run, view='batch')
    assert status['execution']['status'] == 'success'
    assert status['execution']['verified_count'] == 3
    assert status['execution']['unclaimed_count'] == 0
    assert status['batch']['outside_count'] == 1
    assert retention.cleanup(app.store, execute=True)['files'] == 0
    assert evidence.is_file()
    assert app.submit(1, opinion='Never resend', confirm_write=True)['status'] == 'submitted'
    assert len(api.posts) == 3


@pytest.mark.parametrize('state', ['incomplete', 'technical_failure'])
def test_unfinished_review_is_recorded_but_never_written(tmp_path, state):
    app, api = service(tmp_path)
    run = app.pending()['run_id']
    app.collect(run, agent_id='a', expected_task_id=1, agent_workflow_key='travel_expense')
    with pytest.raises(ValueError, match='review_not_complete'):
        app.submit(1, opinion='Must not submit', review_assessment=assessment(app, status=state), confirm_write=True)
    assert api.posts == []
    assert app.inspect(1)['review_assessments'][0]['assessment']['status'] == state
    with pytest.raises(ValueError, match='review_failure_unresolved'):
        app.submit(1, opinion='Do not bypass unfinished review', review_assessment=assessment(app), confirm_write=True)
    app.fail(run, 1, 'synthetic_read_failed')
    app.close(run)
    status = query(tmp_path, 'status', run_id=run, view='batch')
    assert status['execution']['status'] == 'failed'
    assert status['execution']['archive_eligible'] is False


def test_missing_or_wrong_material_assessment_cannot_write(tmp_path):
    app, api = service(tmp_path)
    run = app.pending()['run_id']
    app.collect(run, agent_id='a', expected_task_id=1, agent_workflow_key='travel_expense')
    with pytest.raises(ValueError, match='review_assessment_required'):
        app.submit(1, opinion='Missing assessment', confirm_write=True)
    wrong = {**assessment(app), 'material_sha256': '0' * 64}
    with pytest.raises(ValueError, match='review_material_mismatch'):
        app.submit(1, opinion='Wrong assessment', review_assessment=wrong, confirm_write=True)
    assert api.posts == []


def test_unexplained_early_close_is_visible_and_not_archivable(tmp_path):
    app, _ = service(tmp_path)
    run = app.pending()['run_id']
    app.close(run, handoff=True)
    status = query(tmp_path, 'status', run_id=run, view='batch')
    assert status['execution']['status'] == 'blocked'
    assert status['execution']['stop']['kind'] == 'unexplained'
    assert status['execution']['unclaimed_count'] == 1
    assert status['execution']['archive_eligible'] is False
    assert any(x['code'] == 'batch_stopped_early' for x in status['alerts'])


def test_business_unknown_cannot_be_used_as_batch_read_failure(tmp_path):
    app, _ = service(tmp_path)
    run = app.pending()['run_id']
    with pytest.raises(ValueError, match='stop_read_failure_not_recorded'):
        app.close(run, stop={'kind': 'material_read_failure', 'task_id': 1,
                             'reason': 'A business unknown is not a tool failure'})
    assert app.store.run(run)['status'] == 'running'


def test_early_close_counts_are_stable_after_next_continuation(tmp_path):
    app, api = service(tmp_path)
    api.rows += [{**api.rows[0], 'request_id': str(i)} for i in [102, 103]]
    run = app.pending()['run_id']
    app.collect(run, agent_id='a', expected_task_id=1, agent_workflow_key='travel_expense')
    app.submit(1, opinion='Synthetic completed', review_assessment=assessment(app), confirm_write=True)
    app.close(run, handoff=True)
    before = query(tmp_path, 'status', run_id=run, view='batch')['execution']
    following = app.pending(continue_from=run)['run_id']
    app.collect(following, agent_id='a', expected_task_id=2, agent_workflow_key='travel_expense')
    app.submit(2, opinion='Synthetic completed', review_assessment=assessment(app, 2), confirm_write=True)
    assert query(tmp_path, 'status', run_id=run, view='batch')['execution'] == before
    assert json.loads(app.store.run(run)['summary_json'])['execution']['unclaimed_count'] == 2


@pytest.mark.parametrize('report, observations, state', [
    ({'kind': 'deepseek_attachment_recognition', 'status': 'failed'}, None, 'technical_failure'),
    ({'kind': 'deepseek_attachment_recognition', 'status': 'running'}, None, 'incomplete'),
    ({'kind': 'deepseek_attachment_recognition'}, None, 'incomplete'),
    (None, [{'status': 'failed', 'resource_id': 'file-1', 'locator': 'page:1', 'content': 'Synthetic error'}], 'technical_failure'),
    (None, [{'status': 'unread', 'resource_id': 'file-1', 'locator': 'page:1', 'content': 'Synthetic unread'}], 'incomplete'),
    (None, [{'status': 'unconfirmed', 'resource_id': 'file-1', 'locator': 'page:1', 'content': 'Synthetic unconfirmed'}], 'incomplete'),
])
def test_explicit_read_failure_cannot_be_overridden_by_complete_assessment(tmp_path, report, observations, state):
    app, api = service(tmp_path)
    run = app.pending()['run_id']
    app.collect(run, agent_id='a', expected_task_id=1, agent_workflow_key='travel_expense')
    with pytest.raises(ValueError, match='recognition_not_complete'):
        app.submit(1, opinion='Must not write', review_assessment=assessment(app),
                   recognition=report, observations=observations, confirm_write=True)
    assert api.posts == [] and app.inspect(1)['submissions'] == []
    recorded = app.inspect(1)['review_assessments']
    assert recorded[-1]['assessment']['status'] == state
    with pytest.raises(ValueError, match='review_failure_unresolved'):
        app.submit(1, opinion='Must not bypass by omitting report', review_assessment=assessment(app), confirm_write=True)
    assert api.posts == []


def test_changed_frozen_material_failure_is_recorded_and_stays_blocked(tmp_path):
    app, api = service(tmp_path)
    run = app.pending()['run_id']
    collected = app.collect(run, agent_id='a', expected_task_id=1, agent_workflow_key='travel_expense')
    original = Path(collected['material_file'])
    before = original.read_bytes()
    original.unlink()
    with pytest.raises(ValueError, match='frozen_material_changed'):
        app.submit(1, opinion='Cannot write', review_assessment=assessment(app), confirm_write=True)
    record = app.inspect(1)['review_assessments'][-1]['assessment']
    assert record['status'] == 'technical_failure'
    assert Path(record['evidence_files'][0]).is_file()
    original.write_bytes(before)
    with pytest.raises(ValueError, match='review_failure_unresolved'):
        app.submit(1, opinion='Restoring bytes is not a new review', review_assessment=assessment(app), confirm_write=True)
    assert api.posts == []


def test_context_rotation_requires_real_evidence_and_keeps_its_copy(tmp_path):
    app, _ = service(tmp_path)
    run = app.pending()['run_id']
    evidence = tmp_path / 'platform-context.txt'
    evidence.write_text('Synthetic platform evidence: 61 percent')
    stop = {'kind': 'context_limit', 'reason': 'Synthetic context limit',
            'context_used_percent': 59, 'evidence_file': str(evidence)}
    with pytest.raises(ValueError, match='stop_context_evidence_invalid'):
        app.close(run, stop=stop)
    stop['context_used_percent'] = 61
    result = app.close(run, stop=stop)
    saved = Path(result['execution']['stop']['evidence_file'])
    evidence.unlink()
    assert saved.read_text() == 'Synthetic platform evidence: 61 percent'
    assert query(tmp_path, 'status', run_id=run, view='batch')['execution']['status'] == 'continued'


def test_user_stop_cannot_create_continuation(tmp_path):
    app, _ = service(tmp_path)
    run = app.pending()['run_id']
    with pytest.raises(ValueError, match='stop_handoff_not_allowed'):
        app.close(run, handoff=True, stop={'kind': 'user_stop', 'reason': 'Synthetic stop instruction'})
    assert app.store.run(run)['status'] == 'running'


def test_closed_inflight_review_is_counted_as_unfinished(tmp_path):
    app, _ = service(tmp_path)
    run = app.pending()['run_id']
    app.collect(run, agent_id='a', expected_task_id=1, agent_workflow_key='travel_expense')
    app.close(run)
    execution = query(tmp_path, 'status', run_id=run, view='batch')['execution']
    assert execution['unfinished_count'] == 1
    assert execution['headline'] == '❌ 阻断｜成功 0 单，未处理 1 单'


def test_synthetic_cli_outcome_proof(tmp_path):
    import subprocess
    import sys

    project = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(project / 'scripts/verify-review-outcomes.py'),
                             '--root', str(tmp_path)], cwd=project, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads((tmp_path / 'proof.json').read_text())
    assert proof['synthetic_only'] and proof['business_unknown_refill'] and proof['owner_exited']
    assert proof['execution']['headline'].startswith('❌ 部分失败')
