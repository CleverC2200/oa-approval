
from tests.approvals.source_fakes import POLICY
from datetime import datetime, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from apps.oa.approvals.operations import Approval


class FakeOA:
    def __init__(self):
        self.rows = [{"request_id":"101", "received_at":"2026-10-22 09:00:00",
                      "operator_id":"tester", "workflow_key":"travel_expense"}]
        self.handled = []
        self.posts = []

    def pending_rows(self):
        return list(self.rows)

    def handled_rows(self):
        return list(self.handled)

    def submit_and_verify(self, request_id, opinion):
        self.posts.append((request_id, opinion))
        self.handled.append({"request_id":request_id})
        self.rows = [row for row in self.rows if row["request_id"] != request_id]
        return {"submit_ok":True,"done_hit":True,"postcheck_done_hit":True}


def collect(request_id, *, attachment_root, **kwargs):
    source = Path(attachment_root) / "invoice.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("原件：100.00")
    return {"request_id":request_id,"workflow_key":"travel_expense",
            "declared_fields":[{"path":"detail[0].row[0].field[0]","value":"100.00"}],
            "resources":[{"resource_id":"file-1","name":"发票.txt","local_readonly_path":source}],
            "prepared":{"structured_contents":[{"resource_id":"file-1","key_text":["原件：100.00"]}],"images":[],"manifest":[]}}


def service(tmp_path, api=None):
    api = api or FakeOA()
    return Approval(tmp_path, api=api, writer=api, configs=[], collector=collect, source_policy=POLICY), api


def test_collect_freezes_material_and_default_submit_is_preview(tmp_path):
    app, api = service(tmp_path)
    run = app.pending()
    task = app.collect(run["run_id"], agent_id="travel-agent", expected_task_id=1, agent_workflow_key='travel_expense')
    assert Path(task["material_file"]).is_file()
    assert Path(task["rule_file"]).is_file()
    assert (Path(task["rule_file"]).parent / "../result-contract.md").resolve().is_file()
    result = app.submit(task["task_id"], opinion="AI审核结论：需人工判断 ⚠️")
    assert result["status"] == "preview"
    assert api.posts == []
    details = app.inspect(task["task_id"])
    assert details["attachments"][0]["name"] == "发票.txt"
    assert "100.00" in details["contents"][0]["content"]


def test_collection_failure_preserves_safe_attempt_diagnostics_without_material_or_submission(tmp_path, monkeypatch):
    from apps.oa.approvals import cli
    from apps.oa.approvals.sources import travel as travel_orders
    from apps.oa.approvals.collection.case import CollectionFailure
    from apps.oa.approvals.collection.case import _travel_resources

    def fail_collect(request_id, **_kwargs):
        return _travel_resources(config={'flow_key': 'travel_expense'}, workflow={},
            request_id=request_id, allow_browser_download=True, attachment_timeout_sec=1, travel_source=travel_orders.TravelSource(POLICY))

    monkeypatch.setattr(travel_orders.TravelSource, 'collect_evidence', staticmethod(lambda *_a, **_k: {'errors': [{
        'code': 'travel_attachment_discovery_failed', 'message': 'synthetic-secret-message',
        'counts': {'declared_attachment_count': 1},
        'diagnostics': [{'stage': 'navigation', 'reason': 'navigation_failed', 'browser': 'headless',
                         'exception_type': 'TimeoutError', 'http_status': 503,
                         'source_url': 'https://example.test/?token=synthetic-secret',
                         'message': 'synthetic-secret-message'}]}]}))
    app, api = service(tmp_path)
    app.collector = fail_collect
    run = app.pending()['run_id']
    with pytest.raises(CollectionFailure) as captured:
        app.collect(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')

    output = cli.failure(captured.value)
    report = Path(output['diagnostics_file'])
    body = json.loads(report.read_text())
    assert body['error'] == 'travel_attachment_discovery_failed'
    assert body['counts'] == {'declared_attachment_count': 1}
    assert body['diagnostics'] == [{'stage': 'navigation', 'reason': 'navigation_failed',
        'browser': 'headless', 'exception_type': 'TimeoutError', 'http_status': 503}]
    assert 'synthetic-secret' not in report.read_text()
    task, = app.store.tasks(run)
    assert task['status'] == 'failed' and task['failure_count'] == 1
    assert task['material_file'] is None
    assert app.inspect(task['id'])['submissions'] == []
    assert api.posts == []


def test_diagnostic_file_write_failure_does_not_hide_collection_failure_or_leave_task_busy(tmp_path, monkeypatch):
    from apps.oa.approvals import cli
    from apps.oa.approvals.collection.case import CollectionFailure

    def failed(*_args, **_kwargs):
        raise CollectionFailure('travel_attachment_discovery_failed')

    app, api = service(tmp_path)
    app.collector = failed
    run = app.pending()['run_id']
    monkeypatch.setattr(Path, 'write_text', lambda *_a, **_k: (_ for _ in ()).throw(OSError('synthetic write failure')))
    with pytest.raises(CollectionFailure) as captured:
        app.collect(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    assert cli.failure(captured.value) == {'status': 'failed', 'error': 'travel_attachment_discovery_failed',
                                         'diagnostics_write_failed': True}
    task, = app.store.tasks(run)
    assert task['status'] == 'failed' and task['failure_count'] == 1
    assert api.posts == []


def test_fresh_request_scope_keeps_successful_tasks_out_of_replay(tmp_path):
    app, api = service(tmp_path)
    original = dict(api.rows[0])
    task = app.collect(app.pending()['run_id'], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    assert app.submit(task['task_id'], opinion='合成离线意见', confirm_write=True)['status'] == 'submitted'
    api.rows = [original, {**original, 'request_id': '102'}, {**original, 'request_id': '103'}]
    run = app.pending(request_ids=['101', '102'])
    assert [(row['request_id'], row['status']) for row in run['tasks']] == [
        ('101', 'submitted'), ('102', 'queued')]
    following = app.collect(run['run_id'], agent_id='agent-a', expected_task_id=2, agent_workflow_key='travel_expense')
    assert following['request_id'] == '102'
    assert app.submit(task['task_id'], opinion='不能重发', confirm_write=True)['status'] == 'submitted'
    assert api.posts == [('101', '合成离线意见')]


def test_collect_reports_compact_waiting_for_explicit_filter_and_done_when_empty(tmp_path):
    app, api = service(tmp_path)
    run = app.pending()["run_id"]
    assert app.collect(run, workflow_key="general_expense", agent_id='agent-a', expected_task_id=1, agent_workflow_key='general_expense') == {
        "run_id":run, "status":"waiting", "reason":"workflow_unavailable", "summary":{"queued":1}}
    task = app.collect(run, agent_id="agent-a", expected_task_id=1, agent_workflow_key='travel_expense')
    assert app.collect(run, agent_id="agent-a", expected_task_id=1, agent_workflow_key='travel_expense') == {
        "run_id":run, "status":"waiting", "reason":"agent_busy", "summary":{"reviewing":1}}
    app.submit(task["task_id"], opinion="离线意见")
    assert app.collect(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense') == {
        "run_id":run, "status":"done", "reason":"queue_exhausted", "summary":{"preview":1}}
    assert api.posts == []


@pytest.mark.parametrize("source, expected_source", [("parser", "parser"), ("deepseek", "deepseek"), (None, "deepseek")])
def test_preview_preserves_recognition_source_and_original_report(tmp_path, source, expected_source):
    app, api = service(tmp_path)
    task = app.collect(app.pending()["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    part = {"resource_id":"file-1", "evidence_ref":"material:prepared.structured_contents[0]",
            "answer":"原始金额 001.00，位置 A1"}
    if source is not None:
        part["source"] = source
    report = {"kind":"deepseek_attachment_recognition", "parts":[part]}
    for _ in range(2):
        assert app.submit(task["task_id"], opinion="请核对金额。", recognition=report)["status"] == "preview"
    details = app.inspect(task["task_id"])
    contents = [item for item in details["contents"] if item["locator"] == part["evidence_ref"]]
    assert len(contents) == 1
    assert contents[0]["source"] == expected_source
    assert contents[0]["content"] == "原始金额 001.00，位置 A1"
    assert contents[0]["attachment_id"] == details["attachments"][0]["id"]
    assert len(details["reviews"]) == 1
    assert json.loads(details["reviews"][0]["recognition_json"]) == report
    assert details["submissions"] == []
    assert api.posts == []


def test_default_clients_handle_expired_credentials_between_tasks(tmp_path, monkeypatch):
    from apps.oa.approvals.sources import oa as api
    from apps.oa.approvals.sources import writer as writer_api

    live = FakeOA()
    live.rows.append({**live.rows[0], "request_id": "102", "received_at": "2026-10-22 09:01:00"})
    generation = [0]

    class Reader:
        def __init__(self):
            self.generation = generation[0]

        def pending_rows(self):
            if self.generation != generation[0]:
                raise RuntimeError("pending_queue_fetch_failed")
            return live.pending_rows()

    class Writer:
        def __init__(self):
            self.generation = generation[0]

        def submit_and_verify(self, request_id, opinion):
            if self.generation != generation[0]:
                raise RuntimeError("expired_writer_credentials")
            remaining = [row for row in live.rows if row["request_id"] != request_id]
            result = live.submit_and_verify(request_id, opinion)
            live.rows = remaining
            generation[0] += 1
            return result

    monkeypatch.setattr(api, "build_api", lambda config, **kwargs: Reader())
    monkeypatch.setattr(writer_api, "build_writer_api", lambda **kwargs: Writer())
    app = Approval(tmp_path, configs=[{"flow_key": "travel_expense"}], collector=collect, source_policy=POLICY)
    run = app.pending()["run_id"]
    for request_id in ("101", "102"):
        task = app.collect(run, agent_id='agent-a', expected_task_id=int(request_id) - 100, agent_workflow_key='travel_expense')
        assert task["request_id"] == request_id
        assert app.submit(task["task_id"], opinion="合成离线测试意见", confirm_write=True)["status"] == "submitted"
    assert live.posts == [("101", "合成离线测试意见"), ("102", "合成离线测试意见")]


def test_submit_and_repeat_do_not_send_twice(tmp_path):
    app, api = service(tmp_path)
    task = app.collect(app.pending()["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    first = app.submit(task["task_id"], opinion="请财务核对金额。", confirm_write=True)
    second = app.submit(task["task_id"], opinion="请财务核对金额。", confirm_write=True)
    assert first["status"] == second["status"] == "submitted"
    assert api.posts == [("101","请财务核对金额。")]


def test_changed_original_cannot_be_submitted(tmp_path):
    app, api = service(tmp_path)
    task = app.collect(app.pending()["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    original = Path(app.inspect(task["task_id"])["attachments"][0]["path"])
    original.write_text("changed")
    with pytest.raises(ValueError, match="frozen_material_changed"):
        app.submit(task["task_id"], opinion="审核意见", confirm_write=True)
    assert api.posts == []


def test_replaced_task_instance_cannot_receive_previous_review(tmp_path):
    app, api = service(tmp_path)
    api.rows[0]["task_instance_id"] = "instance-1"
    task = app.collect(app.pending()["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    api.rows[0]["task_instance_id"] = "instance-2"
    with pytest.raises(ValueError, match="submission_target_changed"):
        app.submit(task["task_id"], opinion="审核意见", confirm_write=True)
    assert api.posts == []


def test_unknown_submission_only_allows_readonly_postcheck(tmp_path, monkeypatch):
    from apps.oa.approvals.sources import http as safe_http
    from apps.oa.approvals.sources.writer import FrozenWriterApi

    app, api = service(tmp_path)
    def interrupted(request, **kwargs):
        api.posts.append(request.data)
        raise OSError("connection lost")
    monkeypatch.setattr(safe_http, "urlopen", interrupted)
    app.writer = FrozenWriterApi("fake-token", "fake-user", app_id="fake-app", base_url="https://oa.example.test")
    task = app.collect(app.pending()["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    result = app.submit(task["task_id"],opinion="意见",confirm_write=True)
    assert result["status"] == "unknown"
    fresh = app.pending()
    assert app.collect(fresh["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')["status"] == "done"
    assert app.submit(task["task_id"],opinion="意见",confirm_write=True)["status"] == "unknown"
    api.rows = []
    api.handled = [{"request_id":"101"}]
    assert app.postcheck(task["task_id"])["status"] == "unknown"
    api.handled[0]["handled_at"] = "2020-01-01 12:00:00"
    assert app.postcheck(task["task_id"])["status"] == "unknown"
    assert app.submit(task["task_id"],opinion="意见",confirm_write=True)["status"] == "unknown"
    api.handled[0]["handled_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    api.rows = [{"request_id": "101"}]
    conflict = app.postcheck(task["task_id"])
    assert conflict["status"] == "unknown"
    assert conflict["done_hit"] is True
    assert conflict["submit_ok"] is False
    assert conflict["postcheck_done_hit"] is False
    api.rows = []
    recovered = app.postcheck(task["task_id"])
    assert recovered["status"] == "submitted"
    assert recovered["submit_ok"] is True
    assert recovered["done_hit"] is True
    assert recovered["postcheck_done_hit"] is True
    assert app.submit(task["task_id"], opinion="意见", confirm_write=True)["status"] == "submitted"
    assert len(api.posts) == 1


def test_successful_occurrence_is_not_reset_when_pending_reappears(tmp_path):
    app, api = service(tmp_path)
    original = dict(api.rows[0])
    task = app.collect(app.pending()["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    app.submit(task["task_id"],opinion="意见",confirm_write=True)
    api.rows = [original]
    fresh = app.pending()
    assert fresh["tasks"][0]["status"] == "submitted"
    assert fresh["tasks"][0]["error"] == "submitted_pending_confirmation"
    assert app.collect(fresh["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')["status"] == "done"
    api.rows[0]["received_at"] = "2026-10-23 10:00:00"
    renewed = app.collect(app.pending()["run_id"], agent_id='agent-a', expected_task_id=2, agent_workflow_key='travel_expense')
    assert renewed["task_id"] != task["task_id"]


def test_current_unknown_stops_other_writes_but_new_run_can_process_other_tasks(tmp_path):
    app, api = service(tmp_path)
    api.rows.append({**api.rows[0],"request_id":"102","workflow_key":"general_expense"})
    def collector(request_id, **kwargs):
        material = collect(request_id,**kwargs)
        material["workflow_key"] = "travel_expense" if request_id == "101" else "general_expense"
        return material
    app.collector = collector
    run = app.pending()["run_id"]
    first,second = app.collect(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense'),app.collect(run, agent_id='agent-b', expected_task_id=2, agent_workflow_key='general_expense')
    def interrupted(request_id, opinion):
        api.posts.append(request_id)
        raise OSError("connection lost")
    api.submit_and_verify = interrupted
    assert app.submit(first["task_id"],opinion="意见",confirm_write=True)["status"] == "unknown"
    assert app.collect(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense') == {
        "run_id":run,"status":"blocked","reason":"run_blocked","summary":{"unknown":1,"reviewing":1}}
    with pytest.raises(ValueError, match="run_blocked"):
        app.submit(second["task_id"],opinion="意见",confirm_write=True)
    fresh = app.pending()["run_id"]
    assert app.collect(fresh, agent_id='agent-b', expected_task_id=2, agent_workflow_key='general_expense')["task_id"] == second["task_id"]
    assert api.posts == ["101"]


def test_review_failure_releases_one_slot_without_accessing_oa(tmp_path):
    app, api = service(tmp_path)
    api.rows = [{**api.rows[0], "request_id":str(value)} for value in range(101, 105)]
    run = app.pending()["run_id"]
    tasks = [app.collect(run, agent_id=f"agent-{index}", expected_task_id=index + 1, agent_workflow_key='travel_expense') for index in range(3)]
    assert app.collect(run, agent_id='agent-a', expected_task_id=4, agent_workflow_key='travel_expense') == {
        "run_id":run,"status":"waiting","reason":"inflight_limit","summary":{"reviewing":3,"queued":1}}

    def unavailable():
        raise AssertionError("fail must not access OA")

    api.pending_rows = api.handled_rows = unavailable
    assert app.fail(run, tasks[1]["task_id"], "  native_agent_failed  ") == {
        "run_id":run,"task_id":tasks[1]["task_id"],"status":"failed",
        "reason":"native_agent_failed","failure_count":1}
    details = app.inspect(tasks[1]["task_id"])
    assert details["task"]["error"] == "native_agent_failed"
    assert details["reviews"] == details["submissions"] == []
    assert api.posts == []
    assert app.collect(run, agent_id="agent-1", expected_task_id=4, agent_workflow_key='travel_expense')["request_id"] == "104"


@pytest.mark.parametrize("status", ["queued", "reviewed", "preview", "submitting", "submitted", "unknown", "failed"])
def test_fail_cannot_overwrite_tasks_outside_active_review(tmp_path, status):
    app, api = service(tmp_path)
    run = app.pending()["run_id"]
    task = app.collect(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    app.store.set_status(task["task_id"],status)
    before = app.inspect(task["task_id"])
    with pytest.raises(ValueError, match="task_not_failable"):
        app.fail(run, task["task_id"], "native_agent_failed")
    assert app.inspect(task["task_id"]) == before
    assert api.posts == []


def test_fail_requires_a_running_run_that_owns_the_task(tmp_path):
    app, api = service(tmp_path)
    old_run = app.pending()["run_id"]
    old_task = app.collect(old_run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    api.rows = [{**api.rows[0], "request_id":"102"}]
    current_run = app.pending()["run_id"]
    current_task = app.collect(current_run, agent_id='agent-a', expected_task_id=2, agent_workflow_key='travel_expense')
    with pytest.raises(ValueError, match="run_blocked"):
        app.fail(old_run,old_task["task_id"],"native_agent_failed")
    with pytest.raises(ValueError, match="task_run_mismatch"):
        app.fail(current_run,old_task["task_id"],"native_agent_failed")
    app.close(current_run)
    with pytest.raises(ValueError, match="run_blocked"):
        app.fail(current_run,current_task["task_id"],"native_agent_failed")
    assert app.inspect(old_task["task_id"])["task"]["status"] == "reviewing"
    assert app.inspect(current_task["task_id"])["task"]["status"] == "reviewing"
    assert api.posts == []


def test_completed_later_task_can_submit_and_refill_while_older_reviews_continue(tmp_path):
    app, api = service(tmp_path)
    api.rows = [{**api.rows[0], "request_id":str(value)} for value in range(101, 106)]
    run = app.pending()["run_id"]
    tasks = [app.collect(run,agent_id=f"agent-{index}", expected_task_id=index + 1, agent_workflow_key='travel_expense') for index in range(3)]
    assert app.submit(tasks[2]["task_id"],opinion="离线意见",confirm_write=True)["status"] == "submitted"
    assert [app.inspect(task["task_id"])["task"]["status"] for task in tasks[:2]] == ["reviewing", "reviewing"]
    assert app.collect(run,agent_id="agent-2", expected_task_id=4, agent_workflow_key='travel_expense')["request_id"] == "104"
    assert app.collect(run, agent_id='agent-a', expected_task_id=5, agent_workflow_key='travel_expense') == {
        "run_id":run,"status":"waiting","reason":"inflight_limit",
        "summary":{"reviewing":3,"submitted":1,"queued":1}}
    assert api.posts == [("103","离线意见")]


def test_four_default_previews_release_capacity_and_never_write(tmp_path):
    app, api = service(tmp_path)
    api.rows = [{**api.rows[0], "request_id":str(value)} for value in range(101, 105)]
    run = app.pending()["run_id"]
    for request_id in ("101", "102", "103", "104"):
        task = app.collect(run,agent_id="agent-a", expected_task_id=int(request_id) - 100, agent_workflow_key='travel_expense')
        assert task["request_id"] == request_id
        assert app.submit(task["task_id"],opinion="离线预览意见")["status"] == "preview"
    assert app.collect(run, agent_id='agent-a', expected_task_id=4, agent_workflow_key='travel_expense')["status"] == "done"
    assert api.posts == []


@pytest.mark.parametrize("reason", [None, "", "   ", 123])
def test_fail_requires_a_nonempty_reason(tmp_path, reason):
    app, api = service(tmp_path)
    run = app.pending()["run_id"]
    task = app.store.claim(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')["task"]
    with pytest.raises(ValueError, match="failure_reason_required"):
        app.fail(run,task["id"],reason)
    assert app.inspect(task["id"])["task"]["status"] == "preparing"
    assert app.fail(run,task["id"],"collection_process_failed")["status"] == "failed"
    assert api.posts == []


def test_pending_classifies_raw_overseas_workflow(tmp_path):
    app, api = service(tmp_path)
    from apps.oa.approvals.overseas import WORKFLOW_ID
    from apps.oa.approvals.overseas import WORKFLOW_NAME
    api.rows = [{"request_id":"201","received_at":"2026-10-22 09:00:00","operator_id":"tester",
                 "workflow_id":WORKFLOW_ID,"workflow_name":WORKFLOW_NAME}]
    assert app.pending()["tasks"][0]["workflow_key"] == "overseas_invoice"


@pytest.mark.parametrize("handled_time, expected_status", [("2020-01-01 12:00:00", "unknown"), ("fresh", "submitted")])
def test_overseas_authorized_no_invoice_preserves_fields_and_has_independent_consent(tmp_path, monkeypatch, handled_time, expected_status):
    from apps.oa.approvals.overseas import WORKFLOW_ID
    from apps.oa.approvals.overseas import WORKFLOW_NAME
    from apps.oa.approvals.overseas import RPA_NODE_ID
    from apps.oa.approvals.overseas import digest
    from apps.oa.approvals.sources import official_sql
    api = FakeOA()
    api.rows[0]["workflow_key"] = "overseas_invoice"
    data = {"requestId":"101","workflowId":WORKFLOW_ID,"workflowName":WORKFLOW_NAME,
        "currentNodeId":RPA_NODE_ID,"canEdit":True,
        "workflowMainTableInfo":{"requestRecords":[{"workflowRequestTableFields":[{"fieldName":"bz","fieldValue":"USD"}]}]},
        "workflowDetailTableInfos":[{"tableDBName":"formtable_main_1295_dt1","workflowRequestTableRecords":[
            {"recordOrder":1,"workflowRequestTableFields":[{"fieldName":name,"fieldValue":value,"edit":True}
                for name,value in [("scfp",""),("fpje","75.00"),("fphm","old-number"),("fpdqr","2026-11-01")]]}]}]}
    api.request_context = lambda request_id: (data,[])
    def post(request_id,remark,details,*,fields_by_row):
        assert fields_by_row == {}
        api.posts.append((request_id,details))
        api.rows = []
        api.handled = [{"request_id":request_id,"handled_at":datetime.now(timezone.utc).isoformat() if handled_time == "fresh" else handled_time}]
        return {"code":"SUCCESS"}
    api.submit_invoice = post
    monkeypatch.setattr(official_sql,"confirmed_empty_attachment_fields",lambda *args,**kwargs:
        {"detail[0].row[0].field[0]":{"source":"database_empty","request_id":"101"}})
    app,api = service(tmp_path,api)
    task = app.collect(app.pending()["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='overseas_invoice')
    import json
    material = json.loads(Path(task["material_file"]).read_text())
    recognition = {"material_digest":digest(material),"invoices":[]}
    with pytest.raises(ValueError,match="overseas_authorization_required"):
        app.submit(task["task_id"],recognition=recognition,confirm_write=True)
    result = app.submit(task["task_id"],recognition=recognition,confirm_write=True,allow_overseas=True)
    assert result["status"] == expected_status
    assert result["fields_match"] is True
    assert api.posts == [("101",[])]
    assert data["workflowDetailTableInfos"][0]["workflowRequestTableRecords"][0]["workflowRequestTableFields"][1]["fieldValue"] == "75.00"


def test_bounded_collect_refuses_wrong_agent_and_finishes_without_claiming_outside_prefix(tmp_path):
    app, api = service(tmp_path)
    api.rows = [{**api.rows[0], "request_id":str(value)} for value in range(103, 100, -1)]
    started = app.pending(batch_size=2)
    run = started["run_id"]
    first_id, second_id, outside_id = [task["id"] for task in started["tasks"]]
    before = app.store.tasks(run)
    assert app.collect(run, agent_id="wrong-agent", expected_task_id=first_id,
                       agent_workflow_key="general_expense") == {
        "run_id":run, "status":"refused", "reason":"agent_workflow_mismatch", "summary":{"queued":3}}
    assert app.store.tasks(run) == before
    first = app.collect(run, agent_id="travel-agent", expected_task_id=first_id,
                        agent_workflow_key="travel_expense")
    app.fail(run, first["task_id"], "read_failed")
    second = app.collect(run, agent_id="travel-agent", expected_task_id=second_id,
                         agent_workflow_key="travel_expense")
    assert app.collect(run, agent_id="next-agent", expected_task_id=outside_id, agent_workflow_key='travel_expense') == {
        "run_id":run, "status":"waiting", "reason":"in_flight",
        "summary":{"failed":1,"reviewing":1,"queued":1}}
    app.submit(second["task_id"], opinion="离线预览意见")
    assert app.collect(run, agent_id="next-agent", expected_task_id=outside_id, agent_workflow_key='travel_expense') == {
        "run_id":run, "status":"done", "reason":"batch_exhausted",
        "summary":{"failed":1,"preview":1,"queued":1}}
    assert app.inspect(outside_id)["task"]["status"] == "queued"
    assert api.posts == []


def test_collect_unknown_run_keeps_the_public_not_found_error(tmp_path):
    app, api = service(tmp_path)
    with pytest.raises(ValueError, match="^run_not_found$"):
        app.collect("missing", agent_id="agent-a", expected_task_id=1, agent_workflow_key="travel_expense")
    assert api.posts == []


def test_auth_failure_keeps_attempt_diagnostics_and_never_submits(tmp_path, monkeypatch):
    from apps.oa.approvals import cli
    from apps.oa.approvals.sources import auth
    import base64

    app, fake = service(tmp_path)
    run = app.pending()['run_id']
    app.api = None
    app.configs = [{}]
    values = {'OA_BASE_URL': 'https://oa.example.test', 'OA_APP_ID': 'fake-app',
              'OA_APP_SECRET': 'fake-secret', 'OA_UID': '77',
              'OA_APP_PUBLIC_KEY': base64.b64encode(b'fake-public-key').decode()}
    monkeypatch.setattr(auth, 'env', lambda name, default='': values.get(name, default))
    monkeypatch.setattr(auth, '_openssl_encrypt', lambda *_: 'fake-cipher')
    calls = []

    def token(url, headers, body=''):
        calls.append(url)
        return {'code': -1, 'status': False, 'msg': 'secret解密失败,请检查加密内容.',
                'secret': 'synthetic-secret', 'token': 'synthetic-token'}

    monkeypatch.setattr(auth, '_http_post', token)
    with pytest.raises(auth.AuthFailure) as caught:
        app.collect(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    output = cli.failure(caught.value)
    body = json.loads(Path(output['diagnostics_file']).read_text())
    assert body['error'] == 'oa_auth_applytoken_failed'
    assert body['diagnostics'][0]['reason'] == 'secret_decryption_failed'
    assert body['diagnostics'][0]['api_code'] == -1
    assert not any(value in json.dumps(body) for value in ('synthetic-secret', 'synthetic-token', 'fake-cipher'))
    task, = app.store.tasks(run)
    assert task['status'] == 'failed' and task['material_file'] is None
    assert app.inspect(task['id'])['submissions'] == []
    assert len(calls) == 1 and calls[0].endswith('/applytoken')
    assert fake.posts == []
