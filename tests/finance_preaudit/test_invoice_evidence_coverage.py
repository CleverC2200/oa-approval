from __future__ import annotations

import json

import pytest

from apps.oa_approval import invoice_buyers
from apps.oa_finance_preaudit.case_bundle import collect_case
from tests.finance_preaudit.invoice_fakes import CONFIG, NUMBER, InvoiceApi, linked_field


@pytest.fixture
def cloud_http(monkeypatch):
    payloads = {'101': {
        'number': NUMBER,
        'comm_info': {
            'pro': {'cname': '电子发票（铁路电子客票）', 'type': 47, 'date': '2026-09-01'},
            'price': {'total': '43.00'},
            'buyer': {'company': '示例食品有限公司'},
            'payer': {},
        },
        'flist': [{'id': 'file-101', 'type': 'pdf', 'size': 1024, 'del': 0,
                   'name': 'private-name.pdf', 'url': 'https://private.invalid/file'}],
        'user': {'name': 'private-person'},
        'modify_info': {'pdf': 'https://private.invalid/token'},
    }}

    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return json.dumps({'actionMsg': {'code': '0'}, 'data': self.data}).encode()

    def urlopen(request, **_kwargs):
        data_id = json.loads(request.data)['dataId']
        return Response(payloads[data_id])

    monkeypatch.setattr(invoice_buyers.safe_http, 'urlopen', urlopen)
    return payloads


def collect(api=None):
    api = api or InvoiceApi()
    return collect_case('synthetic-request', oa_api=api, configs=[api.config],
                        allow_browser_download=False)


def test_linked_rail_ticket_retains_authoritative_fields_without_duplicate_upload(cloud_http):
    material = collect()
    entry = material['invoice_evidence']['invoices'][0]
    assert entry['invoice_no'] == NUMBER
    assert entry['data_id'] == '101'
    assert entry['source_field_paths'] == ['detail[0].row[0].field[0]']
    assert entry['fields']['invoice_type'] == {
        'value': '电子发票（铁路电子客票）', 'read_status': 'present',
    }
    assert entry['fields']['total'] == {'value': '43.00', 'read_status': 'present'}
    assert entry['fields']['seller'] == {'value': '', 'read_status': 'not_provided'}
    assert entry['files'] == [{
        'file_id': 'file-101', 'media_type': 'pdf', 'size_bytes': 1024,
        'read_status': 'record_present', 'body_read_status': 'not_read',
    }]
    assert material['resources'] == []
    assert material['prepared']['images'] == []
    assert entry['body_read_status'] == 'not_read'
    assert not any(fact['name'] == '发票销售方' for fact in material['authority_facts'])
    assert not any(sentinel in json.dumps(material) for sentinel in (
        'private-name', 'private-person', 'private.invalid', 'synthetic-token',
    ))


def test_cloud_invoice_number_conflict_never_reaches_trusted_material(cloud_http):
    cloud_http['101']['number'] = '00000000000000000999'
    with pytest.raises(RuntimeError, match='^invoice_buyer_read_failed$'):
        collect()


def test_each_invoice_preserves_exact_multiple_row_binding_and_partial_fields(cloud_http):
    cloud_http['202'] = {'number': '00000000000000000202', 'comm_info': {
        'pro': {'cname': '增值税普通发票'},
        'price': {'total': '74.00'},
    }}
    material = collect(InvoiceApi(rows=[
        [linked_field()], [linked_field()],
        [linked_field('202', '00000000000000000202')],
    ]))
    entries = material['invoice_evidence']['invoices']
    assert [entry['invoice_no'] for entry in entries] == [NUMBER, '00000000000000000202']
    assert entries[0]['source_field_paths'] == [
        'detail[0].row[0].field[0]', 'detail[0].row[1].field[0]',
    ]
    assert entries[1]['source_field_paths'] == ['detail[0].row[2].field[0]']
    assert entries[1]['fields']['issue_date']['read_status'] == 'not_provided'
    assert entries[1]['fields']['buyer']['read_status'] == 'not_provided'
    assert entries[1]['file_records_status'] == 'not_provided'
    assert entries[1]['body_read_status'] == 'not_read'
    facts = {(fact['name'], fact['subject_ref']): fact['value'] for fact in material['authority_facts']}
    assert facts[('发票价税合计', NUMBER)] == '43.00'
    assert facts[('发票价税合计', '00000000000000000202')] == '74.00'
    assert facts[('发票类型', NUMBER)] == '电子发票（铁路电子客票）'
    assert facts[('发票开票日期', NUMBER)] == '2026-09-01'


@pytest.mark.parametrize('config', [
    {'flow_key': 'travel_expense', 'workflow_id': '138023', 'workflow_name': '差旅费报销流程'},
    {'flow_key': 'business_entertainment', 'workflow_id': '138024', 'workflow_name': '业务招待费报销申请流程'},
    {'flow_key': 'business_support_fee', 'workflow_id': '138025', 'workflow_name': '业务支持费报销流程'},
    {'flow_key': 'small_marketing_fee', 'workflow_id': '138026', 'workflow_name': '小额市场费用报销申请流程'},
    CONFIG,
    {'flow_key': 'general_expense', 'workflow_id': '195021', 'workflow_name': '通用费用报销申请流程'},
])
def test_six_finance_collectors_preserve_same_invoice_coverage(config, monkeypatch, cloud_http):
    from apps.oa_approval import travel_orders

    api = InvoiceApi(config=config)
    if config['flow_key'] == 'travel_expense':
        original = api.request_context

        def request_context(request_id):
            workflow, resources = original(request_id)
            workflow['workflowMainTableInfo'] = {'requestRecords': [{
                'workflowRequestTableFields': [{
                    'fieldName': 'lcbh', 'fieldShowName': '流程编号',
                    'fieldShowValue': 'SYNTHETIC-TRAVEL',
                }, {'fieldName': 'fjsl', 'fieldShowName': '附件数量', 'fieldShowValue': '0'}],
            }]}
            return workflow, resources

        api.request_context = request_context
        monkeypatch.setattr(travel_orders, '_ensure_cdp_available', lambda: True)
        monkeypatch.setattr(travel_orders, '_read_trip_candidate_results', lambda *_a, **_k: [{
            'text': 'SYNTHETIC-TRAVEL', 'resources': [], 'attachment_scan_status': 'empty',
        }])
    material = collect_case('synthetic-request', oa_api=api, configs=[config],
                            allow_browser_download=config['flow_key'] == 'travel_expense')
    assert material['invoice_evidence']['invoices'][0]['fields']['total']['value'] == '43.00'
    assert material['invoice_evidence']['invoices'][0]['fields']['seller']['read_status'] == 'not_provided'
    assert any(fact['name'] == '发票价税合计' and fact['value'] == '43.00'
               for fact in material['authority_facts'])


def test_deleted_and_malformed_cloud_files_do_not_become_readable_evidence(cloud_http):
    cloud_http['101']['flist'] = [
        {'id': 'deleted-file', 'type': 'pdf', 'del': '1', 'size': 10},
        {'id': {'token': 'private-file-token'}, 'type': 'pdf', 'del': 0},
        {'id': 'valid-file', 'type': 'jpg', 'del': 0, 'size': True},
        {'type': 'pdf'},
        'not-a-file-record',
    ]
    entry = collect()['invoice_evidence']['invoices'][0]
    assert entry['files'] == [{
        'file_id': 'valid-file', 'media_type': 'jpg',
        'read_status': 'record_present', 'body_read_status': 'not_read',
    }]
    assert 'private-file-token' not in json.dumps(entry)


def test_cloud_number_missing_does_not_replace_oa_link_with_inferred_fact(cloud_http):
    del cloud_http['101']['number']
    entry = collect()['invoice_evidence']['invoices'][0]
    assert entry['invoice_no'] == NUMBER
    assert entry['fields']['invoice_number'] == {'value': '', 'read_status': 'not_provided'}
    assert entry['fields']['total'] == {'value': '43.00', 'read_status': 'present'}

