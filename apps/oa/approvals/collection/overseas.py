from __future__ import annotations

import hashlib
import re
from pathlib import Path

from apps.oa.materials import formats as attachment_extractors
from apps.oa.approvals import forms as evidence_packet
from apps.oa.approvals.sources import official_sql
from apps.oa.approvals import overseas as business
from apps.oa.approvals.collection.downloads import download_resources


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
    prepared = (attachment_extractors.prepare_materials(files,out_dir=directory / "prepared")
        if files else attachment_extractors.PreparedMaterial({"images":[],"manifest":[],"structured_contents":[]}))
    return {"kind":"overseas_invoice_material","request_id":request_id,"workflow_key":"overseas_invoice",
        "snapshot":snap,"workflow":data,"attachments":attachments,
        "resources":[{**item,"local_readonly_path":item["path"]} for item in attachments],
        "prepared":prepared.to_record()}


def download_fresh(resources: list[dict], directory: Path, *, request_id: str) -> list[dict]:
    import fitz

    business._require(isinstance(request_id, str) and re.fullmatch(r'[1-9][0-9]*', request_id) is not None,
             'invoice_request_invalid')
    files, errors = download_resources(
        resources, dest_dir=directory, timeout_sec=60, allow_browser_download=True,
        request_id=request_id, flow_key='overseas_invoice', reuse_cache=False,
    )
    business._require(not errors and len(files) == len(resources), 'invoice_download_failed')
    for item in files:
        path = Path(item['path'])
        business._require(path.suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg'}, 'invoice_attachment_type_unsupported')
        try:
            with fitz.open(path) as document:
                business._require(0 < len(document) <= 100, 'invoice_attachment_unreadable')
                item['pages'] = len(document)
        except Exception:
            raise ValueError('invoice_attachment_unreadable') from None
    return files
