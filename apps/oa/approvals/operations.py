from __future__ import annotations

from apps.oa.approvals.rules import freeze_rules

from contextlib import contextmanager
from functools import partial
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from apps.oa.approvals.collection import cache as attachment_cache
from apps.oa.approvals import finance_precheck, receipts, retention

from apps.oa.approvals import overseas as business
from apps.oa.approvals.collection.overseas import snapshot as overseas_snapshot
from apps.oa.approvals.sources.writer import handled_since

from apps.oa.approvals.store import Store
from apps.oa.approvals.store import encoded
from apps.oa.approvals.store import occurrence_key


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def material_paths(value):
    paths = set()
    if isinstance(value, dict):
        for key,item in value.items():
            if key in {"path","local_readonly_path","source_path"} and isinstance(item,(str,Path)) and item:
                path = Path(item)
                if path.is_absolute():
                    if not path.is_file():
                        raise ValueError("material_file_missing")
                    paths.add(path)
            paths.update(material_paths(item))
    elif isinstance(value,list):
        for item in value:
            paths.update(material_paths(item))
    return paths


def expected_overseas_snapshot(material, plan):
    expected = deepcopy(material["snapshot"])
    for update in plan["updates"]:
        row = next(row for row in expected["rows"] if row["key"] == update["row_key"])
        row["values"].update(update["values"])
    return expected

def check_overseas_fields(api, request_id, material, plan, *, origins):
    data,resources = api.request_context(request_id)
    after = overseas_snapshot(data,resources,request_id,writable=False,origins=origins)
    return business.postcheck_fields_match(after,expected_overseas_snapshot(material,plan))

def submit_overseas(api, writer, request_id, material, plan, *, origins):
    data,resources = api.request_context(request_id)
    current = overseas_snapshot(data,resources,request_id,origins=origins)
    business._require(business.stable_snapshot(current) == business.stable_snapshot(material["snapshot"]),"invoice_target_changed")
    details = business.detail_data(data,current,plan)
    fields_by_row = {tuple(row['key'].rsplit(':', 1)): set(row['field_names'].values())
                     for row in current['rows'] if row.get('field_names')}
    business._require(callable(getattr(writer, "submit_invoice", None)), "invoice_write_adapter_required")
    started_at = datetime.now(timezone.utc).isoformat()
    response = writer.submit_invoice(request_id,plan["remark"],details,fields_by_row=fields_by_row)
    code = str(response.get("code") or "").upper()
    if code != "SUCCESS":
        return {**receipts.verification_result(False,False,submit_ok=False),
            "failure_code":receipts.SUBMIT_REJECTED if code == "ERROR" else "writer_result_unknown"}
    pending = any(str(row.get("request_id")) == request_id for row in api.pending_rows())
    done = handled_since(api.handled_rows(),request_id,started_at)
    fields_match = check_overseas_fields(api,request_id,material,plan,origins=origins)
    result = receipts.verification_result(done,pending,submit_ok=True,fields_match=fields_match)
    return {**result,"failure_code":None if result["postcheck_done_hit"] else receipts.POSTCHECK_TIMEOUT}


class Approval:
    def __init__(self, root, *, api=None, configs=None, collector=None, writer=None, source_policy=None):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root / "approval.sqlite3")
        self.api, self.configs, self.collector, self.writer = api, configs, collector, writer
        self.source_policy = source_policy

    def _api(self):
        if self.api is not None:
            return self.api
        from apps.oa.approvals.sources import oa as api
        from apps.oa.approvals import flows as flow
        self.configs = self.configs if self.configs is not None else flow.load_all_configs()
        return api.build_api(self.configs[0],source_policy=self.source_policy)

    def pending(self, workflows=None, batch_size=None, request_ids=None, continue_from=None, retry_failed=False):
        continuation = self.store.continuation_options(continue_from)
        if continuation is not None:
            if retry_failed or any(value is not None for value in (workflows, batch_size, request_ids)):
                raise ValueError('continuation_selection_changed')
            workflows, batch_size, request_ids = (continuation[key] for key in ('workflows', 'batch_size', 'request_ids'))
        if retry_failed and not request_ids:
            raise ValueError("retry_failed_requires_request_ids")
        rows = []
        for raw in self._api().pending_rows():
            item = dict(raw)
            if request_ids is not None and str(item.get("request_id")) not in request_ids:
                continue
            key = item.get("workflow_key") or item.get("flow_key")
            if not key:
                from apps.oa.approvals import flows as flow
                configs = self.configs if self.configs is not None else flow.load_all_configs()
                matches = [config for config in configs if flow.workflow_matches(
                    {"workflowId":item.get("workflow_id"),"workflowName":item.get("workflow_name")},config)]
                from apps.oa.approvals.overseas import WORKFLOW_ID
                from apps.oa.approvals.overseas import WORKFLOW_NAME
                if str(item.get("workflow_id")) == WORKFLOW_ID or item.get("workflow_name") == WORKFLOW_NAME:
                    matches.append({"flow_key":"overseas_invoice"})
                if len(matches) != 1:
                    continue
                key = matches[0]["flow_key"]
            if workflows and key not in workflows:
                continue
            item["workflow_key"] = key
            rows.append(item)
        run = self.store.start_run(rows,batch_size=batch_size, continue_from=continue_from, retry_failed=retry_failed,
                                  selection={'workflows':workflows, 'batch_size':batch_size, 'request_ids':request_ids})
        cleanup = retention.cleanup(self.store, execute=True)
        result = {"run_id":run,"tasks":self.store.tasks(run)}
        if cleanup["status"] != "completed":
            result["cleanup"] = cleanup
        if batch_size is not None:
            result["batch"] = json.loads(self.store.run(run)["summary_json"])["batch"]
        return result

    def collect(self, run_id, workflow_key=None, agent_id=None, *, expected_task_id=None, agent_workflow_key=None, travel_read_exception=None):
        if travel_read_exception is not None:
            task = self.store.get_task(expected_task_id)
            if (not isinstance(travel_read_exception, dict)
                    or task["workflow_key"] != "travel_expense"
                    or travel_read_exception.get("request_id") != task["request_id"]
                    or not isinstance(travel_read_exception.get("authorization_note"), str)
                    or not travel_read_exception["authorization_note"].strip()):
                raise ValueError("travel_read_exception_invalid")
        decision = self.store.claim(run_id,workflow_key,agent_id,expected_task_id=expected_task_id,
            agent_workflow_key=agent_workflow_key)
        if decision["status"] != "claimed":
            return decision
        task = decision["task"]
        directory = self.root / "materials" / str(task["id"]) / uuid4().hex
        directory.mkdir(parents=True)
        try:
            with attachment_cache.runtime_root(self.root):
                if task["workflow_key"] == "overseas_invoice":
                    from apps.oa.approvals.collection import overseas
                    material = overseas.collect(self._api(),task["request_id"],directory,source_policy=self.source_policy)
                else:
                    if self.collector is None:
                        from apps.oa.approvals.collection.case import collect_case
                        self.collector = partial(collect_case, source_policy=self.source_policy)
                    material = self.collector(task["request_id"],oa_api=self._api(),configs=self.configs or [],
                        allow_browser_download=True,attachment_root=directory / "attachments",prepared_root=directory / "prepared",
                        **({"travel_read_exception": travel_read_exception} if travel_read_exception is not None else {}))
                    if material.get("request_id") != task["request_id"] or material.get("workflow_key") != task["workflow_key"]:
                        raise ValueError("material_identity_changed")
                    if task["workflow_key"] == "tpm_small_marketing_fee":
                        from apps.oa.references import tpm as tpm_price_reference
                        if tpm_price_reference.applies(material):
                            material["tpm_price_reference"] = tpm_price_reference.snapshot(self.root / tpm_price_reference.REFERENCE_PATH)
            rule = freeze_rules(task["workflow_key"],directory / "rules")
            material_file = directory / "material.json"
            material_file.write_text(encoded(material),encoding="utf-8")
            files = material_paths(material) | {material_file} | {path for path in (directory / "rules").rglob("*") if path.is_file()}
            frozen = {str(path):file_digest(path) for path in files}
            frozen_task = self.store.freeze(task["id"],material,material_file,file_digest(material_file),frozen,rule)
            return {"run_id":run_id,"task_id":task["id"],"workflow_key":task["workflow_key"],
                "request_id":task["request_id"],"material_file":str(material_file),"rule_file":str(rule),"rules_root":str(directory / "rules"),"status":frozen_task["status"],
                **({"reason":frozen_task["error"]} if frozen_task["status"] == "failed" else {})}
        except Exception as error:
            from apps.oa.approvals.collection.case import CollectionFailure

            from apps.oa.approvals.sources.auth import AuthFailure

            if isinstance(error, (CollectionFailure, AuthFailure)):
                report = directory / "collection-error.json"
                try:
                    report.write_text(encoded({"error": str(error), "counts": error.counts,
                        "diagnostics": error.diagnostics}), encoding="utf-8")
                    error.diagnostics_file = str(report)
                except OSError:
                    error.diagnostics_write_failed = True
            self.store.fail_collection(task["id"],type(error).__name__+":"+str(error)[:200])
            raise

    def fail(self, run_id, task_id, reason):
        if not isinstance(reason,str) or not reason.strip():
            raise ValueError("failure_reason_required")
        reason = reason.strip()
        task = self.store.fail(task_id,reason,run_id=run_id)
        return {"run_id":run_id,"task_id":task_id,"status":"failed",
            "reason":reason,"failure_count":task["failure_count"]}

    @contextmanager
    def _writer_lock(self):
        with (self.root / "submit.lock").open("a") as stream:
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            yield

    def _frozen(self, task):
        for path,digest in json.loads(task["frozen_json"] or "{}").items():
            if not Path(path).is_file() or file_digest(path) != digest:
                raise ValueError("frozen_material_changed")

    def submit(self, task_id, opinion=None, recognition=None, observations=None, confirm_write=False, allow_overseas=False):
        with self._writer_lock():
            task = self.store.get_task(task_id)
            if task["status"] in {"submitted","unknown","submitting"}:
                return {"task_id":task_id,"status":task["status"],"submissions":self.store.details(task_id)["submissions"]}
            if task["status"] not in {"reviewing","reviewed","preview"}:
                raise ValueError("task_not_reviewable")
            if self.store.run(task["last_run_id"])["status"] != "running":
                raise ValueError("run_blocked")
            self._frozen(task)
            material = json.loads(task["material_json"])
            plan = None
            if task["workflow_key"] == "overseas_invoice":
                plan = business.build_plan(material,recognition)
                opinion = plan["remark"]
                if confirm_write and not allow_overseas:
                    raise ValueError("overseas_authorization_required")
            if not isinstance(opinion,str) or not opinion.strip():
                raise ValueError("opinion_required")
            review = self.store.review(task_id,opinion,recognition,observations)
            delivery = None
            if task['workflow_key'] in finance_precheck.WORKFLOWS and finance_precheck.load_policy() is not None:
                from apps.oa.approvals.sources.prediction import predict
                forecast = predict(task['request_id'], refresh=True)
                delivery = forecast['delivery']
                if forecast['finance_precheck']['status'] == 'not_configured':
                    raise ValueError('finance_precheck_configuration_changed')
            if not confirm_write:
                self.store.set_status(task_id,"preview")
                return {"task_id":task_id,"status":"preview","review_id":review["id"],"opinion":opinion,
                        **({'delivery': delivery} if delivery is not None else {}),
                        **({"field_plan":plan} if plan is not None else {})}
            if delivery is not None and delivery['action'] == 'blocked':
                raise ValueError(delivery['reason'])
            current = self._api().pending_rows()
            matches = [row for row in current if occurrence_key(row) == task["occurrence"]]
            if len(matches) != 1:
                raise ValueError("submission_target_changed")
            if task['workflow_key'] == 'dealer_closure':
                from apps.oa.approvals.sources import dealer_closure
                dealer_closure.validate_submission(self._api(), material)
            writer = self.writer
            if plan is None and writer is None:
                from apps.oa.approvals.sources.writer import build_writer_api
                writer = build_writer_api(source_policy=self.source_policy)
            if delivery is not None and delivery['action'] == 'transfer':
                delivery = writer.prepare_transfer(task['request_id'], delivery)
            submission = self.store.begin_submission(task_id,review["id"],
                result={'delivery': delivery} if delivery is not None else None)
            try:
                if plan is not None:
                    if self.api is None and self.writer is None:
                        from apps.oa.approvals.sources.overseas import build_api
                        read_api = write_api = build_api(source_policy=self.source_policy)
                    else:
                        read_api = self._api()
                        write_api = self.writer if self.writer is not None else self.api
                    result = submit_overseas(read_api,write_api,task["request_id"],material,plan,origins=self.source_policy.attachments)
                elif delivery is not None and delivery['action'] == 'transfer':
                    result = writer.transfer_and_verify(task['request_id'], opinion, delivery)
                else:
                    options = {}
                    if (task['workflow_key'] == 'dealer_closure'
                            and material['dealer_closure_evidence']['submission_context']['delivery_kind'] == 'comment'):
                        options['dealer_material'] = material
                    result = writer.submit_and_verify(task["request_id"],opinion, **options)
            except Exception:
                result = {**receipts.verification_result(False,False,submit_ok=False),"failure_code":"writer_result_unknown"}
            if delivery is not None:
                result['delivery'] = delivery
            status = receipts.submission_status(result)
            self.store.finish_submission(submission,status,result)
            if status == "submitted":
                result["cleanup"] = retention.cleanup(self.store, execute=True, task_id=task_id)
            return {"task_id":task_id,"review_id":review["id"],"status":status,**result}

    def postcheck(self, task_id, *, confirmation_note=None, expected_submission_id=None):
        if confirmation_note is not None and (not isinstance(confirmation_note, str) or not confirmation_note.strip()):
            raise ValueError('confirmation_note_required')
        if (confirmation_note is None) != (expected_submission_id is None):
            raise ValueError('confirmation_submission_required')
        if expected_submission_id is not None and (type(expected_submission_id) is not int or expected_submission_id <= 0):
            raise ValueError('confirmation_submission_required')
        with self._writer_lock():
            task = self.store.get_task(task_id)
            if confirmation_note is not None and task['workflow_key'] == 'overseas_invoice':
                raise ValueError('overseas_confirmation_not_supported')
            if task["status"] == "submitted":
                return {"task_id":task_id,"status":"submitted","submissions":self.store.details(task_id)["submissions"]}
            if task["status"] not in {"unknown","submitting","submitted"}:
                raise ValueError("submission_not_started")
            from apps.oa.approvals.sources.writer import handled_since
            details = self.store.details(task_id)
            submission = details["submissions"][-1]
            delivery = json.loads(submission['result_json']).get('delivery')
            if delivery is not None and delivery['action'] == 'transfer':
                if confirmation_note is not None:
                    raise ValueError('transfer_confirmation_not_supported')
                from apps.oa.approvals.sources.transfer import verify
                sent_review = next(review for review in details['reviews'] if review['id'] == submission['review_id'])
                result = verify(self._api(), task['request_id'], sent_review['opinion'],
                    submission['started_at'], delivery)
                status = receipts.submission_status(result)
                self.store.finish_submission(submission['id'], status, result)
                if status == 'submitted':
                    result['cleanup'] = retention.cleanup(self.store, execute=True, task_id=task_id)
                return {'task_id': task_id, 'status': status, **result}
            if confirmation_note is not None and submission['id'] != expected_submission_id:
                raise ValueError('confirmation_submission_changed')
            pending_rows = self._api().pending_rows()
            pending = any(str(row.get("request_id")) == task["request_id"] for row in pending_rows)
            handled = None
            if task['workflow_key'] == 'dealer_closure':
                material = json.loads(task['material_json'])
                if material['dealer_closure_evidence']['submission_context']['delivery_kind'] == 'comment':
                    from apps.oa.approvals.sources import dealer_closure
                    handled = dealer_closure.feedback_verified(self._api(), material,
                        details['reviews'][-1]['opinion'], submission['started_at'])
            if handled is None:
                handled = handled_since(self._api().handled_rows(),task['request_id'],submission['started_at'])
            fields_match = None
            if task["workflow_key"] == "overseas_invoice":
                review = details["reviews"][-1]
                material = json.loads(task["material_json"])
                fields_match = check_overseas_fields(self._api(),task["request_id"],material,business.build_plan(material,json.loads(review["recognition_json"])),origins=self.source_policy.attachments)
            result = receipts.verification_result(handled,pending,fields_match=fields_match)
            if confirmation_note is not None and receipts.submission_status(result) != 'submitted':
                if handled or sum(occurrence_key(row) == task['occurrence'] for row in pending_rows) != 1:
                    raise ValueError('confirmation_target_changed')
                result.update(failure_code=receipts.USER_CONFIRMED_NOT_WRITTEN,
                    confirmation_note=confirmation_note.strip(), previous_result=json.loads(submission['result_json']))
            status = receipts.submission_status(result)
            self.store.finish_submission(submission["id"],status,result)
            if status == "submitted":
                result["cleanup"] = retention.cleanup(self.store, execute=True, task_id=task_id)
            return {"task_id":task_id,"status":status,**result}

    def inspect(self, task_id):
        return self.store.details(task_id)

    def close(self, run_id, *, handoff=False):
        result = self.store.close(run_id, handoff=handoff)
        cleanup = retention.cleanup(self.store, execute=True)
        if cleanup["status"] != "completed":
            result["cleanup"] = cleanup
        return result
