from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import pytest

from apps.oa.store import Store


def pending(request_id="101", received_at="2026-10-22 09:00:00", workflow_key="travel_expense"):
    return {"request_id": request_id, "received_at": received_at,
            "operator_id": "tester", "workflow_key": workflow_key}


def test_repeated_pending_reuses_task_but_new_delivery_creates_task(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    first = store.start_run([pending()])
    first_id = store.tasks(first)[0]["id"]
    second = store.start_run([pending()])
    assert first_id == store.tasks(second)[0]["id"]
    second_id = store.tasks(second)[0]["id"]
    third = store.start_run([pending(received_at="2026-10-23 09:00:00")])
    assert store.tasks(third)[0]["id"] != second_id
    assert len(store.requests()) == 1


def test_queue_claims_same_type_in_fifo_order_and_limits_three_reviewers(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([
        pending("102", "2026-10-22 09:01:00"),
        pending("101"), pending("103", "2026-10-22 09:02:00", "general_expense"),
        pending("104", "2026-10-22 09:03:00", "business_support_fee"),
        pending("105", "2026-10-22 09:04:00", "small_marketing_fee"),
    ])
    first = store.claim(run, agent_id="agent-a", expected_task_id=2, agent_workflow_key='travel_expense')["task"]
    assert first["request_id"] == "101"
    assert store.claim(run, agent_id="agent-a", expected_task_id=1, agent_workflow_key='travel_expense')["reason"] == "agent_busy"
    assert store.claim(run, agent_id="agent-b", expected_task_id=1, agent_workflow_key='travel_expense')["task"]["request_id"] == "102"
    assert store.claim(run, agent_id="agent-c", expected_task_id=3, agent_workflow_key='general_expense')["task"]["request_id"] == "103"
    assert store.claim(run, agent_id="agent-d", expected_task_id=4, agent_workflow_key='business_support_fee')["reason"] == "inflight_limit"
    store.fail(first["id"], "read_failed")
    assert store.claim(run, agent_id="agent-d", expected_task_id=4, agent_workflow_key='business_support_fee')["task"]["request_id"] == "104"


def test_new_generation_can_claim_the_next_type_after_previous_agent_finishes(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending(), pending("102", workflow_key="general_expense")])
    first = store.claim(run, agent_id="agent-a", expected_task_id=1, agent_workflow_key='travel_expense')["task"]
    store.fail(first["id"], "read_failed")
    assert store.claim(run, agent_id="agent-a", expected_task_id=2, agent_workflow_key='general_expense')["reason"] == "agent_generation_workflow_mismatch"
    assert store.claim(run, agent_id="agent-b", expected_task_id=2, agent_workflow_key='general_expense')["task"]["request_id"] == "102"


@pytest.mark.parametrize("status", ["preparing", "reviewing", "reviewed", "submitting"])
def test_inflight_tasks_hold_capacity_and_their_agent_until_finished(tmp_path, status):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending(str(value)) for value in range(101, 105)])
    first = store.claim(run, agent_id="agent-a", expected_task_id=1, agent_workflow_key='travel_expense')["task"]
    store.set_status(first["id"], status)
    assert store.claim(run, agent_id="agent-a", expected_task_id=2, agent_workflow_key='travel_expense')["reason"] == "agent_busy"
    assert store.claim(run, agent_id="agent-b", expected_task_id=2, agent_workflow_key='travel_expense')["task"]["request_id"] == "102"
    assert store.claim(run, agent_id="agent-c", expected_task_id=3, agent_workflow_key='travel_expense')["task"]["request_id"] == "103"
    assert store.claim(run, agent_id="agent-d", expected_task_id=4, agent_workflow_key='travel_expense')["reason"] == "inflight_limit"
    store.set_status(first["id"], "submitted")
    assert store.claim(run, agent_id="agent-a", expected_task_id=4, agent_workflow_key='travel_expense')["task"]["request_id"] == "104"


def test_fifo_is_stable_for_multiple_instances_and_explicit_filter_is_preserved(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([
        {**pending("102"), "task_instance_id":"later"},
        pending("101", workflow_key="general_expense"),
        {**pending("102"), "task_instance_id":"earlier"},
    ])
    assert [task["id"] for task in store.tasks(run)] == [2, 1, 3]
    filtered = store.claim(run, workflow_key="travel_expense", agent_id="agent-a", expected_task_id=1, agent_workflow_key='travel_expense')["task"]
    assert filtered["id"] == 1
    assert store.claim(run, agent_id="agent-b", expected_task_id=2, agent_workflow_key='general_expense')["task"]["id"] == 2
    assert store.claim(run, agent_id="agent-c", expected_task_id=3, agent_workflow_key='travel_expense')["task"]["id"] == 3


def test_batch_freezes_fifo_prefix_and_failed_claims_keep_their_place(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending("103"), pending("101"), pending("102")], batch_size=2)
    first = store.claim(run, agent_id='agent-a', expected_task_id=2, agent_workflow_key='travel_expense')["task"]
    assert first["request_id"] == "101"
    store.fail(first["id"], "read_failed")
    second = store.claim(run, agent_id='agent-a', expected_task_id=3, agent_workflow_key='travel_expense')["task"]
    assert second["request_id"] == "102"
    store.set_status(second["id"], "preview")
    assert store.claim(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')["reason"] == "batch_exhausted"
    assert store.tasks(run)[-1]["status"] == "queued"
    batch = json.loads(store.run(run)["summary_json"])["batch"]
    assert batch == {"size":2, "task_ids":[first["id"], second["id"]]}
    store.close(run)
    assert json.loads(store.run(run)["summary_json"])["batch"] == batch


@pytest.mark.parametrize("options, reason", [
    ({}, "claim_guards_required"),
    ({"agent_id":"agent-a"}, "claim_guards_required"),
    ({"expected_task_id":True,"agent_workflow_key":"travel_expense","agent_id":"agent-a"}, "claim_guards_required"),
    ({"expected_task_id":1,"agent_workflow_key":" ","agent_id":"agent-a"}, "claim_guards_required"),
    ({"expected_task_id":2,"agent_workflow_key":"travel_expense","agent_id":"agent-a"}, "expected_task_changed"),
    ({"expected_task_id":1,"agent_workflow_key":"general_expense","agent_id":"agent-a"}, "agent_workflow_mismatch"),
    ({"expected_task_id":1,"agent_workflow_key":"travel_expense"}, "agent_identity_required"),
    ({"expected_task_id":1,"agent_id":"agent-a"}, "claim_guards_required"),
    ({"agent_workflow_key":"travel_expense","agent_id":"agent-a"}, "claim_guards_required"),
])
def test_protected_claim_refuses_stale_identity_or_wrong_agent_before_mutation(tmp_path, options, reason):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending(), pending("102", workflow_key="general_expense")])
    before = store.tasks(run)
    assert store.claim(run, **options) == {
        "run_id":run, "status":"refused", "reason":reason, "summary":{"queued":2}}
    assert store.tasks(run) == before
    task = store.claim(run, expected_task_id=1, agent_workflow_key="travel_expense", agent_id="agent-a")["task"]
    assert task["id"] == 1


def test_protected_agent_keeps_type_and_five_claim_budget_after_failures_and_previews(tmp_path):
    path = tmp_path / "approval.sqlite3"
    store = Store(path)
    run = store.start_run([*[pending(str(value)) for value in range(101, 108)],
                           pending("108", workflow_key="general_expense")])
    for index, status in enumerate(("failed", "preview", "failed", "preview", "failed"), 1):
        task = store.claim(run, agent_id="agent-a", expected_task_id=index, agent_workflow_key="travel_expense")["task"]
        assert task["request_id"] == str(100 + index)
        if status == "failed":
            store.fail(task["id"], "read_failed")
        else:
            store.set_status(task["id"], status)
    store.connection.close()
    store = Store(path)
    before = store.tasks(run)
    assert store.claim(run, agent_id="agent-a", expected_task_id=6,
                       agent_workflow_key="travel_expense")["reason"] == "agent_claim_limit"
    assert store.tasks(run) == before
    rotated = store.claim(run, agent_id="agent-b", expected_task_id=6, agent_workflow_key="travel_expense")["task"]
    store.fail(rotated["id"], "read_failed")
    following = store.claim(run, agent_id="agent-b", expected_task_id=7, agent_workflow_key="travel_expense")["task"]
    store.fail(following["id"], "read_failed")
    assert store.claim(run, agent_id="agent-b", expected_task_id=8,
                       agent_workflow_key="general_expense")["reason"] == "agent_generation_workflow_mismatch"
    assert store.get_task(8)["status"] == "queued"
    assert store.claim(run, agent_id="agent-c", expected_task_id=8,
                       agent_workflow_key="general_expense")["task"]["request_id"] == "108"


def test_batch_filter_cannot_replace_oldest_prefix_with_later_matching_type(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending(), pending("102", workflow_key="general_expense")], batch_size=1)
    assert store.claim(run, workflow_key="general_expense", agent_id="general-agent", expected_task_id=2, agent_workflow_key='general_expense')["reason"] == "workflow_unavailable"
    assert [task["status"] for task in store.tasks(run)] == ["queued", "queued"]
    assert store.claim(run, agent_id="travel-agent", expected_task_id=1,
                       agent_workflow_key="travel_expense")["task"]["request_id"] == "101"


def test_protected_expected_identity_is_checked_against_current_sqlite_queue(tmp_path):
    path = tmp_path / "approval.sqlite3"
    first, second = Store(path), Store(path)
    run = first.start_run([pending(), pending("102")])
    first.claim(run, agent_id="agent-a", expected_task_id=1, agent_workflow_key="travel_expense")
    assert second.claim(run, agent_id="agent-b", expected_task_id=1,
                        agent_workflow_key="travel_expense")["reason"] == "expected_task_changed"
    assert second.get_task(2)["status"] == "queued"
    assert second.claim(run, agent_id="agent-b", expected_task_id=2,
                        agent_workflow_key="travel_expense")["task"]["request_id"] == "102"


def test_claim_returns_a_decision_from_the_current_sqlite_snapshot(tmp_path):
    path = tmp_path / "approval.sqlite3"
    first, second = Store(path), Store(path)
    run = first.start_run([pending(), pending("102")], batch_size=1)
    unavailable = second.claim(run, workflow_key="general_expense", agent_id='agent-a', expected_task_id=1, agent_workflow_key='general_expense')
    assert unavailable == {
        "run_id":run, "status":"waiting", "reason":"workflow_unavailable", "summary":{"queued":2}}
    claimed = first.claim(run, agent_id="agent-a", expected_task_id=1, agent_workflow_key='travel_expense')
    assert claimed["status"] == "claimed"
    assert claimed["task"]["request_id"] == "101"
    assert claimed["task"]["status"] == "preparing"
    assert second.claim(run, agent_id="agent-a", expected_task_id=2, agent_workflow_key='travel_expense') == {
        "run_id":run, "status":"waiting", "reason":"agent_busy", "summary":{"preparing":1,"queued":1}}
    first.set_status(claimed["task"]["id"], "preview")
    assert second.claim(run, agent_id='agent-a', expected_task_id=2, agent_workflow_key='travel_expense') == {
        "run_id":run, "status":"done", "reason":"batch_exhausted", "summary":{"preview":1,"queued":1}}
    assert unavailable["summary"] == {"queued":2}


def test_claim_decision_preserves_blocked_guards_and_capacity_priority(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending(str(value)) for value in range(101, 105)])
    for index, agent in enumerate(("agent-a", "agent-b", "agent-c"), 1):
        store.claim(run, agent_id=agent, expected_task_id=index, agent_workflow_key='travel_expense')
    assert store.claim(run, expected_task_id=1)["reason"] == "claim_guards_required"
    assert store.claim(run, agent_id="agent-a", expected_task_id=1,
                       agent_workflow_key="general_expense")["reason"] == "inflight_limit"
    store.close(run)
    assert store.claim(run, expected_task_id=1) == {
        "run_id":run,"status":"blocked","reason":"run_blocked","summary":{"preparing":3,"queued":1}}


def test_two_sqlite_connections_cannot_claim_the_last_slot_twice(tmp_path):
    path = tmp_path / "approval.sqlite3"
    store = Store(path)
    run = store.start_run([pending(str(value)) for value in range(101, 105)])
    store.claim(run, agent_id="agent-a", expected_task_id=1, agent_workflow_key='travel_expense')
    store.claim(run, agent_id="agent-b", expected_task_id=2, agent_workflow_key='travel_expense')
    ready = Barrier(2)

    def claim(agent):
        other = Store(path)
        try:
            ready.wait(timeout=5)
            return other.claim(run, agent_id=agent, expected_task_id=3, agent_workflow_key='travel_expense')
        finally:
            other.connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(claim, agent) for agent in ("agent-c", "agent-d")]
        decisions = [future.result(timeout=10) for future in futures]
    claimed = next(item for item in decisions if item["status"] == "claimed")
    assert claimed["task"]["request_id"] == "103"
    assert next(item for item in decisions if item["status"] != "claimed") == {
        "run_id":run,"status":"waiting","reason":"inflight_limit","summary":{"preparing":3,"queued":1}}
    assert [task["status"] for task in store.tasks(run)] == ["preparing", "preparing", "preparing", "queued"]
