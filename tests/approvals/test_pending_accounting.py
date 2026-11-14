import json

from apps.oa.approvals.client import query
from tests.approvals.test_operations import service
from tests.approvals.review_fixtures import completed_review


def test_excluded_failure_is_still_pending_and_not_archivable(tmp_path):
    app, api = service(tmp_path)
    original = app.pending()['run_id']
    app.collect(original, agent_id='first', expected_task_id=1, agent_workflow_key='travel_expense')
    app.fail(original, 1, 'synthetic_read_failure')
    app.close(original)
    api.rows.append({**api.rows[0], 'request_id': '102', 'workflow_key': 'training'})
    retry = app.pending(workflows=['travel_expense'], batch_size=40)
    assert retry['tasks'] == []
    app.close(retry['run_id'])
    status = query(tmp_path, 'status', run_id=retry['run_id'], view='batch')
    execution = status['execution']
    assert execution['status'] == 'no_eligible'
    assert '无待审' not in execution['headline']
    assert execution['remaining_count'] == 2
    assert execution['selected_remaining_count'] == 1
    assert execution['failure_count'] == 0
    assert execution['archive_eligible'] is False
    assert status['pending']['excluded'] == [{'task_id': 1, 'request_id': '101', 'reason': 'historical_failure'}]
    assert status['pending']['outside_selection_count'] == 1
    assert app.store.get_task(1)['last_run_id'] == original
    assert api.posts == []
    app.store.connection.close()


def test_retry_counts_real_submissions_without_erasing_old_snapshot(tmp_path):
    app, _ = service(tmp_path)
    original = app.pending()['run_id']
    app.collect(original, agent_id='first', expected_task_id=1, agent_workflow_key='travel_expense')
    app.fail(original, 1, 'synthetic_read_failure')
    app.close(original)
    before = json.loads(app.store.run(original)['summary_json'])
    retry = app.pending(request_ids=['101'], retry_failed=True, batch_size=40)
    app.collect(retry['run_id'], agent_id='second', expected_task_id=1, agent_workflow_key='travel_expense')
    app.submit(1, opinion='Synthetic completed', review_assessment=completed_review(app, 1), confirm_write=True)
    app.close(retry['run_id'])
    status = query(tmp_path, 'status', run_id=retry['run_id'], view='batch')
    assert status['execution']['remaining_count'] == 0
    assert status['execution']['verified_count'] == 1
    assert status['pending']['excluded'] == []
    assert json.loads(app.store.run(original)['summary_json']) == before
    app.store.connection.close()


def test_only_actual_empty_queue_gets_empty_headline(tmp_path):
    app, api = service(tmp_path)
    api.rows = []
    run = app.pending(batch_size=40)['run_id']
    app.close(run)
    status = query(tmp_path, 'status', run_id=run, view='batch')
    assert status['execution']['headline'] == '✅ 成功｜无待审单据'
    assert status['execution']['remaining_count'] == 0
    assert status['execution']['archive_eligible'] is True
    app.store.connection.close()


def test_legacy_empty_batch_has_unknown_pending_count_without_rewriting_history(tmp_path):
    app, api = service(tmp_path)
    api.rows = []
    run = app.pending(batch_size=40)['run_id']
    app.close(run)
    legacy = json.loads(app.store.run(run)['summary_json'])
    legacy.pop('pending')
    legacy['execution'].pop('remaining_count')
    legacy['execution'].pop('selected_remaining_count')
    with app.store.connection:
        app.store.connection.execute('UPDATE runs SET summary_json=? WHERE id=?', (json.dumps(legacy), run))
    status = query(tmp_path, 'status', run_id=run, view='batch')
    assert status['pending'] is None
    assert status['execution']['status'] == 'legacy_unverified'
    assert '无待审' not in status['execution']['headline']
    assert status['execution']['remaining_count'] is None
    assert status['execution']['archive_eligible'] is False
    assert json.loads(app.store.run(run)['summary_json']) == legacy
    app.store.connection.close()
