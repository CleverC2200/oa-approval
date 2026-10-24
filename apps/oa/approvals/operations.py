from __future__ import annotations

from apps.oa.runtime.paths import PROJECT_ROOT

from contextlib import contextmanager
import fcntl
import hashlib
import json
import re
from pathlib import Path
import shutil
from uuid import uuid4

from apps.oa.approvals.collection import cache as attachment_cache
from apps.oa.approvals.sources import writer_outcomes

from apps.oa.approvals.store import Store
from apps.oa.approvals.store import encoded
from apps.oa.approvals.store import occurrence_key


PROJECT = PROJECT_ROOT
RULE_NAMES = {
    "travel_expense":"travel-expense", "business_entertainment":"business-entertainment",
    "business_support_fee":"business-support-fee", "small_marketing_fee":"small-marketing-fee",
    "tpm_small_marketing_fee":"tpm-small-marketing-fee", "general_expense":"general-expense",
    "packaging_layout_approval":"packaging-layout-approval",
}


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


def freeze_rules(workflow_key, directory):
    reference = Path(".agents/skills/oa-finance-preaudit/references")
    if workflow_key == "overseas_invoice":
        entry = Path(".agents/skills/oa-overseas-invoice/SKILL.md")
        selected = [entry, Path("docs/overseas-invoice.md")]
    else:
        if workflow_key not in RULE_NAMES:
            raise ValueError("rule_workflow_unknown")
        entry = reference / "flows" / (RULE_NAMES[workflow_key]+".md")
        selected = [entry, *(reference / name for name in (
            "result-contract.md", "observation-contract.md", "execution-contract.md"))]
        if workflow_key == "packaging_layout_approval":
            audit = Path(".agents/skills/audit-chinese-image-text")
            selected += [audit / "SKILL.md", audit / "scripts/problem_set.py"]
            selected += [audit / "references" / name for name in (
                "error-taxonomy.md", "output-contract.md", "problem-set.json", "visual-agent-prompts.md")]
        else:
            selected += [reference / name for name in ("payment-screenshot-exemption.md",
                "finance-result-contract.md", "finance-observation-contract.md")]
            selected += [Path("docs/agents") / name for name in (
                "attachment-recognition.md", "image-compression.md", "invoice-originals.md")]
    for relative in selected:
        source = PROJECT / relative
        if not source.is_file():
            raise ValueError("rule_dependency_missing")
    sources = {(PROJECT / relative).resolve() for relative in selected}
    for source in sources:
        if source.suffix != ".md":
            continue
        for link in re.findall(r"\]\(([^)]+)\)",source.read_text(encoding="utf-8")):
            path = link.split("#",1)[0]
            if path and "://" not in path and (source.parent / path).resolve() not in sources:
                raise ValueError("rule_dependency_out_of_scope")
    for relative in selected:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / relative,target)
    return directory / entry


class Approval:
    def __init__(self, root, *, api=None, configs=None, collector=None, writer=None):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root / "approval.sqlite3")
        self.api, self.configs, self.collector, self.writer = api, configs, collector, writer

    def _api(self):
        if self.api is not None:
            return self.api
        from apps.oa.approvals.sources import oa as api
        from apps.oa.approvals import flows as flow
        self.configs = self.configs if self.configs is not None else flow.load_all_configs()
        return api.build_api(self.configs[0])

    def pending(self, workflows=None, batch_size=None, request_ids=None):
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
        run = self.store.start_run(rows,batch_size=batch_size)
        result = {"run_id":run,"tasks":self.store.tasks(run)}
        if batch_size is not None:
            result["batch"] = json.loads(self.store.run(run)["summary_json"])["batch"]
        return result

    def collect(self, run_id, workflow_key=None, agent_id=None, *, expected_task_id=None, agent_workflow_key=None):
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
                    from apps.oa.approvals import overseas_runtime as overseas
                    material = overseas.collect(self._api(),task["request_id"],directory)
                else:
                    if self.collector is None:
                        from apps.oa.approvals.collection.case import collect_case
                        self.collector = collect_case
                    material = self.collector(task["request_id"],oa_api=self._api(),configs=self.configs or [],
                        allow_browser_download=True,attachment_root=directory / "attachments",prepared_root=directory / "prepared")
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
                from apps.oa.approvals import overseas_runtime as overseas
                plan = overseas.plan(material,recognition)
                opinion = plan["remark"]
                if confirm_write and not allow_overseas:
                    raise ValueError("overseas_authorization_required")
            if not isinstance(opinion,str) or not opinion.strip():
                raise ValueError("opinion_required")
            review = self.store.review(task_id,opinion,recognition,observations)
            if not confirm_write:
                self.store.set_status(task_id,"preview")
                return {"task_id":task_id,"status":"preview","review_id":review["id"],"opinion":opinion}
            current = self._api().pending_rows()
            matches = [row for row in current if occurrence_key(row) == task["occurrence"]]
            if len(matches) != 1:
                raise ValueError("submission_target_changed")
            submission = self.store.begin_submission(task_id,review["id"])
            try:
                if plan is not None:
                    result = overseas.submit(self._api(),task["request_id"],material,plan)
                else:
                    writer = self.writer
                    if writer is None:
                        from apps.oa.approvals.sources.writer import build_writer_api
                        writer = build_writer_api()
                    result = writer.submit_and_verify(task["request_id"],opinion)
            except Exception:
                result = {**writer_outcomes.verification_result(False,False,submit_ok=False),"failure_code":"writer_result_unknown"}
            status = writer_outcomes.submission_status(result)
            self.store.finish_submission(submission,status,result)
            return {"task_id":task_id,"review_id":review["id"],"status":status,**result}

    def postcheck(self, task_id):
        with self._writer_lock():
            task = self.store.get_task(task_id)
            if task["status"] == "submitted":
                return {"task_id":task_id,"status":"submitted","submissions":self.store.details(task_id)["submissions"]}
            if task["status"] not in {"unknown","submitting","submitted"}:
                raise ValueError("submission_not_started")
            from apps.oa.approvals.sources.writer import handled_since
            details = self.store.details(task_id)
            submission = details["submissions"][-1]
            pending = any(str(row.get("request_id")) == task["request_id"] for row in self._api().pending_rows())
            handled = handled_since(self._api().handled_rows(),task["request_id"],submission["started_at"])
            fields_match = None
            if task["workflow_key"] == "overseas_invoice":
                from apps.oa.approvals import overseas_runtime as overseas
                review = details["reviews"][-1]
                material = json.loads(task["material_json"])
                fields_match = overseas.check_fields(self._api(),task["request_id"],material,overseas.plan(material,json.loads(review["recognition_json"])))
            result = writer_outcomes.verification_result(handled,pending,fields_match=fields_match)
            status = writer_outcomes.submission_status(result)
            self.store.finish_submission(submission["id"],status,result)
            return {"task_id":task_id,"status":status,**result}

    def inspect(self, task_id):
        return self.store.details(task_id)

    def close(self, run_id):
        return self.store.close(run_id)
