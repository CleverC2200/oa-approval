from __future__ import annotations


import tempfile


import json


import shutil


from pathlib import Path


from unittest.mock import patch


import zipfile


import fitz


import pytest


from apps import json_artifact


from apps.oa.approvals.collection import downloads as attachments
from apps.oa.materials import formats as attachment_extractors
from apps.oa.approvals import forms as evidence_packet
from apps.oa.approvals import flows as flow
from apps.oa.approvals.sources import invoices as invoice_buyers
from apps.oa.approvals.sources import travel as travel_orders


from apps.oa.approvals.collection import case as case_bundle

from apps.oa.approvals.collection.case import collect_case


FLOW_CONFIGS = (
    {
        "flow_key": "travel_expense",
        "workflow_id": "138023",
        "workflow_name": "差旅费报销流程",
    },
    {
        "flow_key": "business_entertainment",
        "workflow_id": "138024",
        "workflow_name": "业务招待费报销申请流程",
    },
    {
        "flow_key": "business_support_fee",
        "workflow_id": "138025",
        "workflow_name": "业务支持费报销流程",
    },
    {
        "flow_key": "small_marketing_fee",
        "workflow_id": "138026",
        "workflow_name": "小额市场费用报销申请流程",
    },
    {
        "flow_key": "tpm_small_marketing_fee",
        "workflow_id": "195039",
        "workflow_name": "TPM小额市场费用报销申请流程",
    },
    {
        "flow_key": "general_expense",
        "workflow_id": "195021",
        "workflow_name": "通用费用报销申请流程",
    },
    {
        "flow_key": "packaging_layout_approval",
        "workflow_id": "198521",
        "workflow_name": "包装新制（更改）及包装版面审批流程",
    },
)


@pytest.mark.parametrize("config", FLOW_CONFIGS, ids=lambda value: value['flow_key'])
def test_image_encoding_policy_is_applied_at_collection(tmp_path, monkeypatch, config):
    resource = {"id": "resource-001", "name": "scan.pdf", "downloadUrl": "/download/scan.pdf"}
    monkeypatch.setattr(case_bundle, "_travel_resources", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(case_bundle.evidence_packet, "logical_attachment_resources", lambda *_: [resource])
    monkeypatch.setattr(case_bundle.attachments, "download_resources", lambda *_a, **_k: ([{
        "resource_id": "resource-001", "name": "scan.pdf", "path": str(tmp_path / "scan.pdf"),
    }], []))
    captured = []

    def prepare(_downloads, *, out_dir, compact_pdf=False, compact_spreadsheet=False):
        captured.append((compact_pdf, compact_spreadsheet))
        return [], [], []

    monkeypatch.setattr(case_bundle.attachment_extractors, "prepare_materials", prepare)
    monkeypatch.setattr(case_bundle.invoice_buyers, "read_invoice_buyers", lambda *_a, **_k: {
        "read_status": "not_applicable", "invoices": [],
    })
    collect_case("recorded-request", oa_api=RecordedOaApi(config["workflow_id"], config["workflow_name"]),
                 configs=FLOW_CONFIGS, allow_browser_download=False,
                 attachment_root=tmp_path / "attachments", prepared_root=tmp_path / "prepared")
    assert captured == [(config['flow_key'] == 'general_expense', config['flow_key'] == 'tpm_small_marketing_fee')]


class RecordedOaApi:
    def __init__(self, workflow_id: str, workflow_name: str) -> None:
        self._workflow_id = workflow_id
        self._workflow_name = workflow_name

    def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
        return (
            {
                "workflowBaseInfo": {
                    "workflowId": self._workflow_id,
                    "workflowName": self._workflow_name,
                },
                "workflowMainTableInfo": {
                    "requestRecords": [
                        {
                            "workflowRequestTableFields": [
                                {
                                    "fieldName": "amount",
                                    "fieldShowName": "报销金额",
                                    "fieldShowValue": "100.00",
                                }
                            ]
                        }
                    ]
                },
                "workflowDetailTableInfos": [
                    {
                        "workflowRequestTableRecords": [
                            {
                                "workflowRequestTableFields": [
                                    {
                                        "fieldName": "expense_type",
                                        "fieldShowName": "费用类型",
                                        "fieldShowValue": "酒店",
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
            [],
        )


@pytest.mark.parametrize("config", FLOW_CONFIGS)
@pytest.mark.parametrize("company_browser", ["browser.5534", "browser.38009", "browser.19001"])
def test_collection_preserves_populated_fields_and_reading_state(config: dict, company_browser: str) -> None:
    class FieldApi(RecordedOaApi):
        def request_context(self, request_id: str) -> tuple[dict, list]:
            workflow, resources = super().request_context(request_id)
            workflow["workflowMainTableInfo"]["requestRecords"][0][
                "workflowRequestTableFields"
            ] = [{
                "fieldName": "bxgs", "fieldShowName": "报销公司",
                "fieldHtmlType": "3", "fieldType": "226",
                "fieldDBType": company_browser, "fieldValue": "示例食品有限公司",
                "fieldShowValue": "",
            }, {
                "fieldName": "zfgs", "fieldShowName": "支付公司",
                "fieldHtmlType": "3", "fieldType": "161", "fieldValue": "11",
                "fieldShowValue": "<a>示例食品有限公司</a>&nbsp;",
            }]
            workflow["workflowDetailTableInfos"][0]["workflowRequestTableRecords"][0][
                "workflowRequestTableFields"
            ] = [{
                "fieldName": "sapkhbm", "fieldShowName": "SAP客户编码",
                "fieldHtmlType": "3", "fieldType": "226",
                "fieldDBType": "browser.6003", "fieldValue": "0000123456",
                "fieldShowValue": "<span>[该字段暂不支持]</span>",
            }, {
                "fieldName": "fycdzt", "fieldShowName": "费用承担主体",
                "fieldHtmlType": "3", "fieldType": "4", "fieldValue": "1794",
                "fieldShowValue": "",
            }]
            return workflow, resources

    with (
        patch.object(case_bundle.official_sql, "travel_extra_fields", return_value=[]),
        patch.object(invoice_buyers, "read_invoice_buyers", return_value={
            "read_status": "not_applicable", "invoices": [],
        }),
        patch.object(travel_orders, "collect_evidence", return_value={
            "fields": [], "resources": [], "errors": [],
        }),
    ):
        material = collect_case(
            "recorded-request", oa_api=FieldApi(config["workflow_id"], config["workflow_name"]),
            configs=FLOW_CONFIGS, allow_browser_download=False,
        )
    fields = material["declared_fields"]
    assert [item["value"] for item in fields] == [
        "示例食品有限公司", "示例食品有限公司", "0000123456", "",
    ]
    assert [item["value_source"] for item in fields] == ["raw", "display", "raw", "none"]
    assert [item["read_status"] for item in fields] == ["present", "present", "present", "unresolved"]
    assert fields[2]["path"] == "detail[0].row[0].field[0]"


@pytest.mark.parametrize(("field", "value", "status", "source"), [
    ({"fieldHtmlType": "1", "fieldValue": 0, "fieldShowValue": ""}, "0", "present", "raw"),
    ({"fieldHtmlType": "1", "fieldValue": "2026-01-02", "fieldShowValue": "&nbsp;"}, "2026-01-02", "present", "raw"),
    ({"fieldValue": "", "fieldShowValue": ""}, "", "empty", "none"),
    ({"fieldValue": None, "fieldShowValue": "<span>[该字段暂不支持]</span>"}, "", "unresolved", "none"),
    ({"fieldHtmlType": "3", "fieldValue": "11", "fieldShowValue": None}, "", "unresolved", "none"),
    ({"fieldHtmlType": "5", "fieldValue": "0", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldName": "bxgs", "fieldShowName": "报销公司", "fieldHtmlType": "3", "fieldType": "226", "fieldValue": "11", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldName": "bxgs", "fieldShowName": "报销公司", "fieldHtmlType": "3", "fieldType": "226", "fieldDBType": "browser.19001", "fieldValue": "11", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldName": "bxgs", "fieldShowName": "报销公司", "fieldHtmlType": "3", "fieldType": "161", "fieldDBType": "browser.19001", "fieldValue": "示例食品有限公司", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldName": "other", "fieldShowName": "其他字段", "fieldHtmlType": "3", "fieldType": "226", "fieldDBType": "browser.19001", "fieldValue": "示例食品有限公司", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldName": "sapkhbm", "fieldShowName": "SAP客户编码", "fieldHtmlType": "3", "fieldType": "161", "fieldValue": "11", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldShowName": "发票号码", "fieldValue": "12345", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldShowName": "关联发票", "fieldValue": "12345", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldHtmlType": "3", "fieldValue": "11", "fieldShowValue": "<a>示例公司</a>&nbsp"}, "示例公司", "present", "display"),
    ({"fieldValue": "001", "fieldShowValue": "000045[该字段仅支持显示]"}, "000045", "present", "display"),
])
def test_field_resolution_does_not_confuse_missing_values_and_internal_ids(
    field: dict, value: str, status: str, source: str,
) -> None:
    workflow = {"workflowMainTableInfo": {"requestRecords": [{"workflowRequestTableFields": [field]}]}}
    entry = evidence_packet.extract_fields(workflow)[0]
    assert (entry["value"], entry["read_status"], entry["value_source"]) == (value, status, source)
    assert entry["raw_value"] == ("" if field.get("fieldValue") is None else str(field["fieldValue"]))


def _company_workflow(bxgs: dict, bxgsmc: dict | None) -> dict:
    fields = [bxgs]
    if bxgsmc is not None:
        fields.append(bxgsmc)
    return {
        "workflowMainTableInfo": {
            "requestRecords": [{"workflowRequestTableFields": fields}]
        }
    }


def test_company_field_uses_companion_name_when_browser_display_is_empty() -> None:
    workflow = _company_workflow(
        {
            "fieldName": "bxgs", "fieldShowName": "报销公司",
            "fieldHtmlType": "3", "fieldType": "226",
            "fieldDBType": "browser.9999", "fieldValue": "11", "fieldShowValue": "",
        },
        {
            "fieldName": "bxgsmc", "fieldShowName": "报销公司名称",
            "fieldHtmlType": "1", "fieldValue": "示例食品有限公司", "fieldShowValue": "",
        },
    )

    fields = evidence_packet.extract_fields(workflow)

    assert (fields[0]["value"], fields[0]["read_status"], fields[0]["value_source"]) == (
        "示例食品有限公司",
        "present",
        "companion",
    )
    assert fields[0]["raw_value"] == "11"


@pytest.mark.parametrize("companion", [
    None,
    {
        "fieldName": "bxgsmc", "fieldShowName": "报销公司名称",
        "fieldHtmlType": "1", "fieldValue": "", "fieldShowValue": "",
    },
])
def test_company_field_stays_unresolved_without_a_populated_companion(
    companion: dict | None,
) -> None:
    workflow = _company_workflow(
        {
            "fieldName": "bxgs", "fieldShowName": "报销公司",
            "fieldHtmlType": "3", "fieldType": "226",
            "fieldDBType": "browser.9999", "fieldValue": "11", "fieldShowValue": "",
        },
        companion,
    )

    fields = evidence_packet.extract_fields(workflow)

    assert (fields[0]["value"], fields[0]["read_status"]) == ("", "unresolved")


def test_company_field_does_not_fall_back_to_a_present_browser_display() -> None:
    workflow = _company_workflow(
        {
            "fieldName": "bxgs", "fieldShowName": "报销公司",
            "fieldHtmlType": "3", "fieldType": "226",
            "fieldDBType": "browser.5534", "fieldValue": "示例食品有限公司",
            "fieldShowValue": "示例食品有限公司",
        },
        {
            "fieldName": "bxgsmc", "fieldShowName": "报销公司名称",
            "fieldHtmlType": "1", "fieldValue": "其他公司", "fieldShowValue": "",
        },
    )

    fields = evidence_packet.extract_fields(workflow)

    assert (fields[0]["value"], fields[0]["value_source"]) == (
        "示例食品有限公司",
        "display",
    )


def test_archive_limit_error_reaches_flow_admission_without_preparing_partial_files(tmp_path, monkeypatch):
    archive = tmp_path / "too-many.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for index in range(attachments.ARCHIVE_MAX_FILES + 1):
            output.writestr(f"{index}.txt", "recorded")
    resource = {"id": "recorded-archive", "name": archive.name, "downloadUrl": "/recorded/too-many.zip"}
    monkeypatch.setattr(case_bundle, "_travel_resources", lambda **_kw: ([], []))
    monkeypatch.setattr(case_bundle, "_authority_facts", lambda **_kw: [])
    monkeypatch.setattr(evidence_packet, "logical_attachment_resources", lambda *_: [resource])
    monkeypatch.setattr(attachments, "_download_resource_direct", lambda *_a, **_kw: (archive, ""))
    monkeypatch.setattr(attachments.runtime_control, "stop_requested", lambda: False)
    prepared = []
    monkeypatch.setattr(attachment_extractors, "prepare_materials", lambda *_a, **_kw: prepared.append(True))
    with pytest.raises(RuntimeError, match="^attachment_archive_failed$"):
        collect_case("recorded-request", oa_api=RecordedOaApi("195039", "TPM小额市场费用报销申请流程"),
                     configs=FLOW_CONFIGS, allow_browser_download=True,
                     attachment_root=tmp_path / "attachments", prepared_root=tmp_path / "prepared")
    assert prepared == []

