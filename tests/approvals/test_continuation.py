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


def test_authorized_failure_precedes_new_work_and_is_not_retried_again_in_continuation(tmp_path):
    app = create_demo(tmp_path)
    app.api.rows[0]['received_at'] = '2026-10-28 12:00:00'
    first = app.pending(request_ids=[app.api.rows[0]['request_id']], batch_size=1)
    failed = finish_one(app, first, fail=True)
    material = app.store.get_task(failed['id'])['material_file']
    app.close(first['run_id'])
    recovery = app.pending(batch_size=1, recovery_request_ids=[failed['request_id']])
    assert recovery['batch']['task_ids'] == [failed['id']]
    assert recovery['tasks'][0]['id'] == failed['id']
    result = finish_one(app, recovery, fail=True)
    assert result['id'] == failed['id']
    assert app.store.get_task(failed['id'])['failure_count'] == 2
    from pathlib import Path

    assert Path(material).is_file()
    assert app.close(recovery['run_id'], handoff=True)['handoff']['status'] == 'ready'
    next_run = app.pending(continue_from=recovery['run_id'])
    assert failed['id'] not in [task['id'] for task in next_run['tasks']]
    assert next_run['batch']['task_ids']
    assert app.store.get_task(failed['id'])['status'] == 'failed'
    assert app.api.handled == []


def test_recovery_requires_exact_authorized_failure_and_keeps_unrelated_failures_excluded(tmp_path):
    app = create_demo(tmp_path)
    first = app.pending(batch_size=3)
    failed = finish_one(app, first, fail=True)
    another = next(task for task in app.store.tasks(first['run_id']) if task['status'] == 'queued')
    app.collect(first['run_id'], agent_id='second', expected_task_id=another['id'], agent_workflow_key=another['workflow_key'])
    app.fail(first['run_id'], another['id'], 'synthetic_second_failure')
    app.close(first['run_id'])
    current = app.pending(batch_size=2, recovery_request_ids=[failed['request_id'], 'not-in-pending'])
    assert failed['id'] in current['batch']['task_ids']
    assert another['id'] not in [task['id'] for task in current['tasks']]
    assert app.store.get_task(another['id'])['status'] == 'failed'
    with pytest.raises(ValueError, match='recovery_request_ids_invalid'):
        app.pending(recovery_request_ids=[''])


def test_recovery_does_not_requeue_unknown_or_successful_attempts(tmp_path):
    app = create_demo(tmp_path)
    first = app.pending(batch_size=1)
    task = finish_one(app, first)
    app.close(first['run_id'])
    app.store.set_status(task['id'], 'unknown')
    run = app.pending(recovery_request_ids=[task['request_id']], batch_size=1)
    assert app.store.get_task(task['id'])['status'] == 'unknown'
    assert task['id'] not in run['batch']['task_ids']


def test_ready_continuation_can_prioritize_newly_authorized_failure_once(tmp_path):
    app = create_demo(tmp_path)
    first = app.pending(batch_size=1)
    failed = finish_one(app, first, fail=True)
    assert app.close(first['run_id'], handoff=True)['handoff']['status'] == 'ready'
    recovery = app.pending(continue_from=first['run_id'], recovery_request_ids=[failed['request_id']])
    assert recovery['batch']['task_ids'] == [failed['id']]
    assert finish_one(app, recovery, fail=True)['id'] == failed['id']
    assert app.close(recovery['run_id'], handoff=True)['handoff']['status'] == 'ready'
    following = app.pending(continue_from=recovery['run_id'], recovery_request_ids=[failed['request_id']])
    assert failed['id'] not in following['batch']['task_ids']
    assert app.store.get_task(failed['id'])['failure_count'] == 2


@pytest.mark.parametrize('with_old_review', [False, True])
def test_unclaimed_recovery_keeps_priority_across_multiple_small_batches(tmp_path, with_old_review):
    app = create_demo(tmp_path)
    recovery_ids = [row['request_id'] for row in app.api.rows[1:]]
    original = app.pending(request_ids=recovery_ids)
    failed_ids = []
    for index, task in enumerate(original['tasks']):
        app.collect(original['run_id'], agent_id=f'original-{index}', expected_task_id=task['id'], agent_workflow_key=task['workflow_key'])
        if with_old_review and index == 1:
            from tests.approvals.review_fixtures import completed_review

            pending_rows = app.api.pending_rows

            def unavailable():
                raise RuntimeError('synthetic_prewrite_read_failure')

            app.api.pending_rows = unavailable
            with pytest.raises(RuntimeError, match='synthetic_prewrite_read_failure'):
                app.submit(task['id'], opinion='Synthetic retained opinion', confirm_write=True,
                           review_assessment=completed_review(app, task['id']))
            app.api.pending_rows = pending_rows
        else:
            app.fail(original['run_id'], task['id'], 'synthetic_read_failure')
        failed_ids.append(task['id'])
    app.close(original['run_id'])
    if with_old_review:
        details = app.store.details(failed_ids[1])
        app.store.fail_closed_review(original['run_id'], failed_ids[1], details['reviews'][-1]['id'], 'confirmed_prewrite_failure')
    run = app.pending(batch_size=1, recovery_request_ids=recovery_ids)
    first = finish_one(app, run, fail=True)
    assert first['id'] == failed_ids[0]
    assert app.close(run['run_id'], handoff=True)['handoff']['status'] == 'ready'
    successor = app.pending(continue_from=run['run_id'])
    assert successor['batch']['task_ids'] == [failed_ids[1]]
    assert finish_one(app, successor, fail=True)['id'] == failed_ids[1]
    assert app.close(successor['run_id'], handoff=True)['handoff']['status'] == 'ready'
    normal = app.pending(continue_from=successor['run_id'])
    assert all(task['id'] not in failed_ids for task in normal['tasks'])
    assert normal['tasks'][0]['request_id'] == app.api.rows[0]['request_id']


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


@pytest.mark.parametrize('legacy_state', ['preview', 'queued', 'reviewed'])
def test_initial_refresh_keeps_unsubmitted_preview_out_but_accepts_new_delivery(tmp_path, legacy_state):
    app = create_demo(tmp_path)
    first = app.pending(batch_size=1)
    task = finish_one(app, first)
    original = app.store.details(task['id'])
    app.close(first['run_id'])
    app.store.set_status(task['id'], legacy_state)
    fresh = app.pending(batch_size=1)
    assert task['id'] not in [item['id'] for item in fresh['tasks']]
    assert fresh['batch']['task_ids'] == [fresh['tasks'][0]['id']]
    assert app.store.details(task['id'])['reviews'] == original['reviews']
    assert app.store.details(task['id'])['submissions'] == []
    app.close(fresh['run_id'])
    app.api.rows[0]['received_at'] = '2026-10-26 12:00:00'
    new_delivery = app.pending(batch_size=1)
    replacement = next(item for item in new_delivery['tasks'] if item['request_id'] == task['request_id'])
    assert replacement['id'] != task['id']
    assert replacement['status'] == 'queued'
