from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from apps.oa_approval import attachment_extractors, evidence_packet, official_sql
from apps.oa_approval import overseas_invoice as business
from apps.oa_approval.overseas_invoice_api import download_fresh
from apps.oa_approval.writer_api import handled_since


def snapshot(data, resources, request_id, *, writable=True):
    try:
        return business.snapshot(data,resources,writable=writable)
    except ValueError as error:
        if str(error) not in {"invoice_attachment_unbound","invoice_attachments_missing"}:
            raise
    empty = official_sql.confirmed_empty_attachment_fields(data,request_id=request_id,workflow_key="overseas_invoice")
    return business.snapshot(data,resources,writable=writable,empty_fields=empty)


def collect(api, request_id, directory):
    data,resources = api.request_context(request_id)
    snap = snapshot(data,resources,request_id)
    business._require(snap["request_id"] == request_id,"invoice_identity_mismatch")
    selected_ids = {str(item["id"]) for item in snap["resources"]}
    selected = [item for item in evidence_packet.logical_attachment_resources(data,resources) if str(item.get("id")) in selected_ids]
    files = download_fresh(selected,directory / "attachments",request_id=request_id) if selected else []
    attachments = [{"resource_id":str(item["resource_id"]),"name":item.get("name") or Path(item["path"]).name,
        "path":str(Path(item["path"]).absolute()),"sha256":hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest(),"pages":item["pages"]} for item in files]
    business._require({item["resource_id"] for item in attachments} == selected_ids,"invoice_download_incomplete")
    images,manifest,structured = attachment_extractors.prepare_materials(files,out_dir=directory / "prepared") if files else ([],[],[])
    return {"kind":"overseas_invoice_material","request_id":request_id,"workflow_key":"overseas_invoice",
        "snapshot":snap,"workflow":data,"attachments":attachments,
        "resources":[{**item,"local_readonly_path":item["path"]} for item in attachments],
        "prepared":{"images":images,"manifest":manifest,"structured_contents":structured}}


def plan(material, recognition):
    return business.build_plan(material,recognition)


def expected_snapshot(material, plan):
    expected = deepcopy(material["snapshot"])
    for update in plan["updates"]:
        row = next(row for row in expected["rows"] if row["key"] == update["row_key"])
        row["values"].update(update["values"])
    return expected


def check_fields(api, request_id, material, plan):
    data,resources = api.request_context(request_id)
    after = snapshot(data,resources,request_id,writable=False)
    return business.postcheck_fields_match(after,expected_snapshot(material,plan))


def submit(api, request_id, material, plan):
    data,resources = api.request_context(request_id)
    current = snapshot(data,resources,request_id)
    business._require(business.stable_snapshot(current) == business.stable_snapshot(material["snapshot"]),"invoice_target_changed")
    details = business.detail_data(data,current,plan)
    fields_by_row = {tuple(row['key'].rsplit(':', 1)): set(row['field_names'].values())
                     for row in current['rows'] if row.get('field_names')}
    if not hasattr(api,"submit_invoice"):
        from apps.oa_approval.overseas_invoice_api import build_api
        api = build_api()
    started_at = datetime.now(timezone.utc).isoformat()
    response = api.submit_invoice(request_id,plan["remark"],details,fields_by_row=fields_by_row)
    code = str(response.get("code") or "").upper()
    if code != "SUCCESS":
        return {"submit_ok":False,"done_hit":False,"postcheck_done_hit":False,
            "failure_code":"writer_submit_rejected" if code == "ERROR" else "writer_result_unknown"}
    pending = any(str(row.get("request_id")) == request_id for row in api.pending_rows())
    done = handled_since(api.handled_rows(),request_id,started_at)
    fields_match = check_fields(api,request_id,material,plan)
    return {"submit_ok":True,"done_hit":done,"postcheck_done_hit":done and not pending and fields_match,
        "fields_match":fields_match,"failure_code":None if done and not pending and fields_match else "writer_postcheck_timeout"}
