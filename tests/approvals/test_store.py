from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
from pathlib import Path
from threading import Barrier

import pytest

from apps.oa.approvals.store import Store


def pending(request_id='101', received_at='2026-10-22 09:00:00', workflow_key='travel_expense'):
    return {'request_id': request_id, 'received_at': received_at, 'operator_id': 'tester', 'workflow_key': workflow_key}


def freeze(store, run, task_id):
    claimed = store.claim(run, expected_task_id=task_id)
    assert claimed['status'] == 'claimed'
    path = store.path.parent / f'{task_id}.json'
    material = {'prepared': {'images': [], 'structured_contents': [{'resource_id': 'text', 'text': 'Synthetic'}], 'manifest': []}}
    path.write_text(json.dumps(material))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return store.freeze(task_id, material, path, digest, {str(path): digest}, path)


def dispatch(store, run, task_id, agent='agent-a', workflow='travel_expense'):
    return store.dispatch_review(run, expected_task_id=task_id, agent_id=agent, agent_workflow_key=workflow)


def test_repeated_pending_reuses_task_but_new_delivery_creates_task(tmp_path):
    store = Store(tmp_path / 'approval.sqlite3')
    first = store.start_run([pending()])
    first_id = store.tasks(first)[0]['id']
    second = store.start_run([pending()])
    assert first_id == store.tasks(second)[0]['id']
    third = store.start_run([pending(received_at='2026-10-23 09:00:00')])
    assert store.tasks(third)[0]['id'] != first_id
    assert len(store.requests()) == 1


def test_preparation_claim_uses_global_fifo_and_frozen_batch_prefix(tmp_path):
    store = Store(tmp_path / 'approval.sqlite3')
    run = store.start_run([pending('103'), pending('101'), pending('102', workflow_key='general_expense')], batch_size=2)
    assert store.claim(run, expected_task_id=1)['reason'] == 'expected_task_changed'
    first = store.claim(run, expected_task_id=2)['task']
    assert first['request_id'] == '101' and first['agent_id'] is None
    assert store.claim(run, expected_task_id=3)['reason'] == 'preparation_buffer_full'
    store.fail(first['id'], 'read_failed')
    assert store.claim(run, expected_task_id=3)['task']['request_id'] == '102'
    store.fail(3, 'read_failed')
    assert store.claim(run, expected_task_id=1)['reason'] == 'batch_exhausted'
    assert store.get_task(1)['status'] == 'queued'


@pytest.mark.parametrize('options', [{}, {'expected_task_id': True}, {'expected_task_id': 0}])
def test_preparation_requires_exact_task_guard_before_mutation(tmp_path, options):
    store = Store(tmp_path / 'approval.sqlite3')
    run = store.start_run([pending()])
    before = store.tasks(run)
    assert store.claim(run, **options)['reason'] == 'claim_guards_required'
    assert store.tasks(run) == before


@pytest.mark.parametrize('options,reason', [
    ({}, 'claim_guards_required'),
    ({'expected_task_id': True, 'agent_workflow_key': 'travel_expense', 'agent_id': 'a'}, 'claim_guards_required'),
    ({'expected_task_id': 1, 'agent_workflow_key': 'travel_expense'}, 'agent_identity_required'),
    ({'expected_task_id': 2, 'agent_workflow_key': 'travel_expense', 'agent_id': 'a'}, 'expected_task_changed'),
    ({'expected_task_id': 1, 'agent_workflow_key': 'general_expense', 'agent_id': 'a'}, 'agent_workflow_mismatch'),
])
def test_review_dispatch_checks_identity_and_frozen_task_before_mutation(tmp_path, options, reason):
    store = Store(tmp_path / 'approval.sqlite3')
    run = store.start_run([pending()])
    freeze(store, run, 1)
    before = store.tasks(run)
    assert store.dispatch_review(run, **options)['reason'] == reason
    assert store.tasks(run) == before


def test_review_slots_only_hold_reviewing_agents_and_preserve_generation_workflow(tmp_path):
    store = Store(tmp_path / 'approval.sqlite3')
    run = store.start_run([*[pending(str(value)) for value in range(101, 105)], pending('105', workflow_key='general_expense')])
    freeze(store, run, 1)
    dispatch(store, run, 1)
    freeze(store, run, 2)
    assert dispatch(store, run, 2)['reason'] == 'agent_busy'
    dispatch(store, run, 2, 'agent-b')
    freeze(store, run, 3)
    dispatch(store, run, 3, 'agent-c')
    freeze(store, run, 4)
    assert dispatch(store, run, 4, 'agent-d')['reason'] == 'inflight_limit'
    store.fail(1, 'read_failed')
    assert dispatch(store, run, 4)['status'] == 'claimed'
    freeze(store, run, 5)
    store.fail(4, 'read_failed')
    assert dispatch(store, run, 5, workflow='general_expense')['reason'] == 'agent_generation_workflow_mismatch'
    assert dispatch(store, run, 5, 'new-agent', 'general_expense')['status'] == 'claimed'


def test_two_connections_cannot_claim_the_preparation_buffer_twice(tmp_path):
    path = tmp_path / 'approval.sqlite3'
    store = Store(path)
    run = store.start_run([pending(), pending('102')])
    barrier = Barrier(2)
    def claim():
        other = Store(path)
        try:
            barrier.wait(timeout=5)
            return other.claim(run, expected_task_id=1)
        finally:
            other.connection.close()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [future.result(timeout=10) for future in [workers.submit(claim), workers.submit(claim)]]
    assert sorted(result['status'] for result in results) == ['claimed', 'waiting']
    assert next(result for result in results if result['status'] == 'waiting')['reason'] == 'preparation_buffer_full'


def test_two_connections_cannot_dispatch_the_last_slot_twice(tmp_path):
    path = tmp_path / 'approval.sqlite3'
    store = Store(path)
    run = store.start_run([pending(str(value)) for value in range(101, 105)])
    for task_id in (1, 2):
        freeze(store, run, task_id)
        dispatch(store, run, task_id, f'agent-{task_id}')
    freeze(store, run, 3)
    barrier = Barrier(2)
    def claim(agent):
        other = Store(path)
        try:
            barrier.wait(timeout=5)
            return dispatch(other, run, 3, agent)
        finally:
            other.connection.close()
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(claim, agent) for agent in ('agent-c', 'agent-d')]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(result['status'] for result in results) == ['claimed', 'waiting']
    assert next(result for result in results if result['status'] == 'waiting')['reason'] == 'inflight_limit'
    assert store.pipeline_capacity(store.tasks(run))['reviewing'] == 3


def test_closed_run_refuses_preparation_and_review_dispatch(tmp_path):
    store = Store(tmp_path / 'approval.sqlite3')
    run = store.start_run([pending()])
    store.close(run)
    assert store.claim(run, expected_task_id=1)['reason'] == 'run_blocked'
    assert dispatch(store, run, 1)['reason'] == 'run_blocked'
    assert Path(store.path).is_file()
