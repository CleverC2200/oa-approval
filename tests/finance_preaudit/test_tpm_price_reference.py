from __future__ import annotations


import copy


import hashlib


import json


from pathlib import Path
from types import SimpleNamespace


import pytest


from openpyxl import Workbook


from apps.oa.references import tpm as prices


def _catalog(tmp_path: Path) -> dict:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "参考报价单"
    sheet.append(["序号", "类型", "材料名称", "材料编码", "材质说明/要求", "单位", "未税指导价（元）", "含税指导价（元，1%）"])
    sheet.append([1, "合成测试", "测试板", "001", "5mm", "㎡", 99, 20])
    path = tmp_path / "source.xlsx"
    workbook.save(path)
    return prices.read_workbook(path)


def _material(subject: str = "终端建设费-终端形象制作费") -> dict:
    return {"declared_fields": [{"code": "yskm", "path": "detail[0].row[0].field[2]", "read_status": "present", "value": subject}]}


def test_import_preserves_price_column_units_and_source(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    assert catalog["rows"] == [{"source_row": 2, "material_code": "001", "name": "测试板", "specification": "5mm", "unit": "㎡", "guide_price": "20"}]
    assert catalog["price_column"] == "H"
    assert len(catalog["source_sha256"]) == 64
    assert catalog["sheet"] == "参考报价单"


def test_collect_includes_local_reference_in_frozen_material(tmp_path: Path) -> None:
    from apps.oa.approvals.operations import Approval

    catalog = _catalog(tmp_path)
    reference = tmp_path / prices.REFERENCE_PATH
    reference.parent.mkdir()
    reference.write_text(json.dumps(catalog))
    api = SimpleNamespace(pending_rows=lambda: [{"request_id": "101", "operator_id": "tester",
        "received_at": "2026-10-22 09:00:00", "workflow_key": "tpm_small_marketing_fee"}])
    app = Approval(tmp_path, api=api, configs=[], collector=lambda request_id, **kwargs: {
        **_material(), "request_id": request_id, "workflow_key": "tpm_small_marketing_fee",
        "resources": [], "prepared": {"images": [], "structured_contents": []}})
    task = app.collect(app.pending()["run_id"], agent_id='agent-a', expected_task_id=1, agent_workflow_key='tpm_small_marketing_fee')
    frozen = json.loads(Path(task["material_file"]).read_text())["tpm_price_reference"]
    assert frozen["status"] == "ready"
    assert frozen["rows"] == catalog["rows"]
    assert frozen["source_sha256"] == catalog["source_sha256"]
    assert frozen["catalog_sha256"] == hashlib.sha256(reference.read_bytes()).hexdigest()
    assert app.inspect(task["task_id"])["submissions"] == []


@pytest.mark.parametrize("fault", ["negative", "nan", "duplicate", "missing_spec", "wrong_column"])
def test_invalid_catalog_is_a_price_gap_only(tmp_path: Path, fault: str) -> None:
    catalog = _catalog(tmp_path)
    if fault == "negative":
        catalog["rows"][0]["guide_price"] = "-1"
    elif fault == "nan":
        catalog["rows"][0]["guide_price"] = "NaN"
    elif fault == "duplicate":
        catalog["rows"].append(dict(catalog["rows"][0]))
    elif fault == "missing_spec":
        del catalog["rows"][0]["specification"]
    else:
        catalog["price_column"] = "G"
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(catalog))
    assert prices.snapshot(path)["status"] == "unreadable"
    assert prices.snapshot(tmp_path / "absent.json") == {"status": "not_configured"}


def test_scope_is_exact_and_row_driven() -> None:
    assert prices.applies(_material())
    assert not prices.applies(_material("推广费用-物料费"))
    assert not prices.applies({"declared_fields": [], "title": "终端建设费-终端形象制作费"})
    mixed = _material("推广费用-物料费")
    mixed["declared_fields"] += _material()["declared_fields"]
    assert prices.applies(mixed)


def test_pass_requires_a_real_frozen_catalog_row(tmp_path: Path) -> None:
    material = {**_material(), "tpm_price_reference": {"status": "ready", **_catalog(tmp_path)}}
    item = {"item_key": "attachment_completeness", "status": "pass", "conclusion": "附件完整。", "evidence_refs": ["material:tpm_price_reference.rows.999"], "missing_evidence": []}
    assert prices.evidence_gap(item, material)
    item["evidence_refs"] = ["material:tpm_price_reference.rows.0"]
    assert prices.evidence_gap(item, material) is None
    item["item_key"] = "reimbursable_minimum"
    assert prices.evidence_gap(item, material) is None


@pytest.mark.parametrize("status", ["fail", "uncertain"])
def test_reference_guard_keeps_existing_business_problems(status: str) -> None:
    item = {"item_key": "attachment_completeness", "status": status}
    assert prices.evidence_gap(item, _material()) is None

