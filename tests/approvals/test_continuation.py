import json

import pytest

from apps.oa.approvals.demo import create_demo


def finish_one(app, started, *, fail=False):
    run_id = started['run_id']
    task = next(task for task in started['tasks'] if task['status'] == 'queued')
    collected = app.collect(run_id, agent_id=f"reviewer-{run_id}", expected_task_id=task['id'],
                            agent_workflow_key=task['workflow_key'])
    assert collected['status'] == 'reviewing'
    if fail:
        app.fail(run_id, task['id'], 'synthetic_read_failure')
    else:
        assert app.submit(task['id'], opinion='Synthetic preview')['status'] == 'preview'
    return task


def test_continuation_drains_preview_and_failed_tasks_once_without_expanding_selection(tmp_path):
    app = create_demo(tmp_path)
    started = app.pending(workflows=['travel_expense', 'general_expense'], batch_size=1)
    first = finish_one(app,started,fail=True)
    closed = app.close(started['run_id'],handoff=True)
    assert closed['handoff']['status'] == 'ready'
    assert closed['handoff']['remaining'] == 1
    assert closed['handoff']['runtime_root'] == str(tmp_path)
    called = app.api.pending_rows
    app.api.pending_rows = lambda: pytest.fail('pending must not refresh while a continuation is reserved')
    with pytest.raises(ValueError,match='continuation_pending'):
        app.pending()
    with pytest.raises(ValueError,match='continuation_selection_changed'):
        app.pending(continue_from=started['run_id'],workflows=['business_entertainment'])
    app.api.pending_rows = called
    successor = app.pending(continue_from=started['run_id'])
    assert successor['batch']['size'] == 1
    assert [task['workflow_key'] for task in successor['tasks']] == ['general_expense']
    assert app.store.get_task(first['id'])['status'] == 'failed'
    app.api.pending_rows = lambda: pytest.fail('consumed continuations must stop before OA reads')
    with pytest.raises(ValueError,match='continuation_not_available'):
        app.pending(continue_from=started['run_id'])
    finish_one(app,successor)
    assert app.close(successor['run_id'],handoff=True)['handoff'] == {'status':'done','remaining':0}


def test_preview_chain_carries_processed_occurrences_across_multiple_conversations(tmp_path):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    seen = []
    for _ in range(3):
        seen.append(finish_one(app,started)['id'])
        closed = app.close(started['run_id'],handoff=True)
        if closed['handoff']['status'] == 'done':
            break
        started = app.pending(continue_from=started['run_id'])
    assert len(set(seen)) == 3
    assert closed['handoff'] == {'status':'done','remaining':0}
    assert all(app.store.get_task(task_id)['status'] == 'preview' for task_id in seen)


@pytest.mark.parametrize('state', ['unknown','reviewing','submitted','no_progress'])
def test_handoff_closes_owner_but_refuses_unresolved_unverified_or_empty_work(tmp_path,state):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    if state != 'no_progress':
        task = started['tasks'][0]
        app.store.set_status(task['id'],state)
    closed = app.close(started['run_id'],handoff=True)
    assert closed['status'] == 'closed'
    assert closed['handoff']['status'] == 'blocked'
    assert 'start_arguments' not in closed['handoff']
    with pytest.raises(ValueError,match='continuation_not_available'):
        app.pending(continue_from=started['run_id'])


def test_new_pending_delivery_is_not_hidden_by_previous_processed_identity(tmp_path):
    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    first = finish_one(app,started)
    assert app.close(started['run_id'],handoff=True)['handoff']['status'] == 'ready'
    app.api.rows[0]['received_at'] = '2026-10-26 12:00:00'
    successor = app.pending(continue_from=started['run_id'])
    repeated = next(task for task in successor['tasks'] if task['request_id'] == first['request_id'])
    assert repeated['id'] != first['id']
    assert repeated['status'] == 'queued'
    summary = json.loads(app.store.run(started['run_id'])['summary_json'])
    assert summary['handoff']['next_run_id'] == successor['run_id']
