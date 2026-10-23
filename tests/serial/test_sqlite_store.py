import json

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
    first = store.claim(run, agent_id="agent-a")
    assert first["request_id"] == "101"
    assert store.claim(run, agent_id="agent-a") is None
    assert store.claim(run, agent_id="agent-b")["request_id"] == "102"
    assert store.claim(run, agent_id="agent-c")["request_id"] == "103"
    assert store.claim(run, agent_id="agent-d") is None
    store.fail(first["id"], "read_failed")
    assert store.claim(run, agent_id="agent-d")["request_id"] == "104"


def test_idle_agent_can_claim_the_next_type_without_historical_binding(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending(), pending("102", workflow_key="general_expense")])
    first = store.claim(run, agent_id="agent-a")
    store.fail(first["id"], "read_failed")
    assert store.claim(run, agent_id="agent-a")["request_id"] == "102"


@pytest.mark.parametrize("status", ["preparing", "reviewing", "reviewed", "submitting"])
def test_inflight_tasks_hold_capacity_and_their_agent_until_finished(tmp_path, status):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending(str(value)) for value in range(101, 105)])
    first = store.claim(run, agent_id="agent-a")
    store.set_status(first["id"], status)
    assert store.claim(run, agent_id="agent-a") is None
    assert store.claim(run, agent_id="agent-b")["request_id"] == "102"
    assert store.claim(run, agent_id="agent-c")["request_id"] == "103"
    assert store.claim(run, agent_id="agent-d") is None
    store.set_status(first["id"], "submitted")
    assert store.claim(run, agent_id="agent-a")["request_id"] == "104"


def test_fifo_is_stable_for_multiple_instances_and_explicit_filter_is_preserved(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([
        {**pending("102"), "task_instance_id":"later"},
        pending("101", workflow_key="general_expense"),
        {**pending("102"), "task_instance_id":"earlier"},
    ])
    assert [task["id"] for task in store.tasks(run)] == [2, 1, 3]
    filtered = store.claim(run, workflow_key="travel_expense", agent_id="agent-a")
    assert filtered["id"] == 1
    assert store.claim(run, agent_id="agent-b")["id"] == 2
    assert store.claim(run, agent_id="agent-c")["id"] == 3


def test_batch_freezes_fifo_prefix_and_failed_claims_keep_their_place(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending("103"), pending("101"), pending("102")], batch_size=2)
    first = store.claim(run)
    assert first["request_id"] == "101"
    store.fail(first["id"], "read_failed")
    second = store.claim(run)
    assert second["request_id"] == "102"
    store.set_status(second["id"], "preview")
    assert store.claim(run) is None
    assert store.tasks(run)[-1]["status"] == "queued"
    batch = json.loads(store.run(run)["summary_json"])["batch"]
    assert batch == {"size":2, "task_ids":[first["id"], second["id"]]}
    store.close(run)
    assert json.loads(store.run(run)["summary_json"])["batch"] == batch


@pytest.mark.parametrize("options, reason", [
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
    with pytest.raises(ValueError, match=reason):
        store.claim(run, **options)
    assert store.tasks(run) == before
    task = store.claim(run, expected_task_id=1, agent_workflow_key="travel_expense", agent_id="agent-a")
    assert task["id"] == 1


def test_protected_agent_keeps_type_and_five_claim_budget_after_failures_and_previews(tmp_path):
    path = tmp_path / "approval.sqlite3"
    store = Store(path)
    run = store.start_run([*[pending(str(value)) for value in range(101, 108)],
                           pending("108", workflow_key="general_expense")])
    for index, status in enumerate(("failed", "preview", "failed", "preview", "failed"), 1):
        task = store.claim(run, agent_id="agent-a", expected_task_id=index, agent_workflow_key="travel_expense")
        assert task["request_id"] == str(100 + index)
        if status == "failed":
            store.fail(task["id"], "read_failed")
        else:
            store.set_status(task["id"], status)
    store.connection.close()
    store = Store(path)
    before = store.tasks(run)
    with pytest.raises(ValueError, match="agent_claim_limit"):
        store.claim(run, agent_id="agent-a", expected_task_id=6, agent_workflow_key="travel_expense")
    assert store.tasks(run) == before
    rotated = store.claim(run, agent_id="agent-b", expected_task_id=6, agent_workflow_key="travel_expense")
    store.fail(rotated["id"], "read_failed")
    following = store.claim(run, agent_id="agent-b", expected_task_id=7, agent_workflow_key="travel_expense")
    store.fail(following["id"], "read_failed")
    with pytest.raises(ValueError, match="agent_generation_workflow_mismatch"):
        store.claim(run, agent_id="agent-b", expected_task_id=8, agent_workflow_key="general_expense")
    assert store.get_task(8)["status"] == "queued"
    assert store.claim(run, agent_id="agent-c", expected_task_id=8,
                       agent_workflow_key="general_expense")["request_id"] == "108"


def test_batch_filter_cannot_replace_oldest_prefix_with_later_matching_type(tmp_path):
    store = Store(tmp_path / "approval.sqlite3")
    run = store.start_run([pending(), pending("102", workflow_key="general_expense")], batch_size=1)
    assert store.claim(run, workflow_key="general_expense", agent_id="general-agent") is None
    assert [task["status"] for task in store.tasks(run)] == ["queued", "queued"]
    assert store.claim(run, agent_id="travel-agent", expected_task_id=1,
                       agent_workflow_key="travel_expense")["request_id"] == "101"


def test_protected_expected_identity_is_checked_against_current_sqlite_queue(tmp_path):
    path = tmp_path / "approval.sqlite3"
    first, second = Store(path), Store(path)
    run = first.start_run([pending(), pending("102")])
    first.claim(run, agent_id="agent-a", expected_task_id=1, agent_workflow_key="travel_expense")
    with pytest.raises(ValueError, match="expected_task_changed"):
        second.claim(run, agent_id="agent-b", expected_task_id=1, agent_workflow_key="travel_expense")
    assert second.get_task(2)["status"] == "queued"
    assert second.claim(run, agent_id="agent-b", expected_task_id=2,
                        agent_workflow_key="travel_expense")["request_id"] == "102"
