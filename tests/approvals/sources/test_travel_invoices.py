import json
from types import SimpleNamespace

import pytest

from apps.oa.approvals.sources import travel_invoices
from apps.oa.approvals.sources.travel_evidence import CandidateObservation, TravelEvidence
from tests.approvals.sources.test_travel import browser_transport, collect_travel


def expense(**changes):
    return {'cpbh': '0600', 'ddlx': '06001', 'sjly': '2', 'ywdh': 'TR-SYNTHETIC',
            'id': 'expense-a', 'dbid': 'detail-a', 'ygid': 'person-a', 'bxdh': 'SYNTHETIC-TRAVEL', **changes}


def order():
    return {'status': '200', 'result': {'ddbh': 'TR-SYNTHETIC',
        'ckxxVOList': [{'id': 'passenger-a', 'cxrYgid': 'person-a', 'cxrXm': '示例乘客'}],
        'invoiceList': [{'id': 'invoice-a', 'mxid': 'passenger-a', 'cxrxm': '示例乘客',
                         'appStatus': '3', 'invoiceNo': '00000000000000000001',
                         'invoiceUrl': '123/invoice.ofd', 'fpje': '43.00',
                         'fptt': '示例公司', 'nsrsbh': 'SYNTHETIC-TAX'}]}}


def test_automatic_source_adds_order_originals_without_changing_manual_inventory(browser_transport):
    browser_transport.outcomes[True] = {'expense_rows': [expense(), expense(id='expense-b')], 'hotel_payload': order()}
    browser_transport.cookies = [{'domain': 'trip.example.test', 'name': 'session', 'value': 'private-cookie'}]
    evidence = collect_travel(count='0')
    assert evidence.errors == ()
    target, = evidence.download_targets
    assert target['id'] == 'travel_platform:123'
    assert target['downloadUrl'] == 'https://trip.example.test/fcfile/fcc/opera/getFile/123/invoice.ofd'
    assert target['_ephemeral_cookie_header'] == 'session=private-cookie'
    assert evidence.attachment_scan_record()['status'] == 'empty'
    facts = [json.loads(item['value']) for item in evidence.facts if item['name'] == '商旅订单发票']
    assert [item['expense_id'] for item in facts] == ['expense-a', 'expense-b']
    assert all(item['invoices'][0]['resource_id'] == target['id'] for item in facts)
    assert 'private-cookie' not in json.dumps(evidence.facts)
    assert len(browser_transport.requests) == 1
    url, kwargs = browser_transport.requests[0]
    assert url.endswith(travel_invoices.TRAIN_DETAIL_PATH)
    assert json.loads(kwargs['data']) == {'ddbh': 'TR-SYNTHETIC', 'czlx': ''}
    assert kwargs['headers']['Content-Type'] == 'application/json'
    assert all(page.closed for browser in browser_transport.browsers for page in browser.pages)


@pytest.mark.parametrize('fault,code', [
    ('order', 'order_mismatch'), ('passenger', 'passenger_mismatch'),
    ('file', 'file_invalid'), ('missing_file', 'file_invalid'), ('response', 'response_invalid'),
])
def test_untrusted_order_invoice_stops_collection(browser_transport, fault, code):
    payload = order()
    result = payload['result']
    if fault == 'order':
        result['ddbh'] = 'OTHER'
    elif fault == 'passenger':
        result['invoiceList'][0]['mxid'] = 'OTHER'
    elif fault == 'file':
        result['invoiceList'][0]['invoiceUrl'] = 'https://other.invalid/123/a.ofd?secret=value'
    elif fault == 'missing_file':
        result['invoiceList'][0]['invoiceUrl'] = ''
    else:
        payload['status'] = '500'
    browser_transport.outcomes[True] = {'expense_rows': [expense()], 'hotel_payload': payload}
    evidence = collect_travel(count='0')
    assert evidence.errors[0].code == 'travel_order_invoice_' + code
    assert evidence.download_targets == ()
    assert 'secret' not in str(evidence.errors)


def test_other_employee_invoices_are_not_collected(browser_transport):
    payload = order()
    payload['result']['ckxxVOList'].append({'id': 'passenger-b', 'cxrYgid': 'person-b', 'cxrXm': '其他乘客'})
    payload['result']['invoiceList'].append({**payload['result']['invoiceList'][0], 'mxid': 'passenger-b', 'cxrxm': '其他乘客', 'invoiceUrl': '456/other.ofd'})
    browser_transport.outcomes[True] = {'expense_rows': [expense()], 'hotel_payload': payload}
    evidence = collect_travel(count='0')
    assert evidence.errors == ()
    assert [item['id'] for item in evidence.download_targets] == ['travel_platform:123']


def test_unissued_invoice_is_preserved_without_requesting_issue_or_download(browser_transport):
    payload = order()
    payload['result']['invoiceList'][0].update(appStatus='1', invoiceUrl='')
    browser_transport.outcomes[True] = {'expense_rows': [expense()], 'hotel_payload': payload}
    evidence = collect_travel(count='0')
    assert evidence.errors == ()
    assert evidence.download_targets == ()
    fact, = [json.loads(item['value']) for item in evidence.facts if item['name'] == '商旅订单发票']
    assert fact['invoices'][0]['appStatus'] == '1'
    assert len(browser_transport.requests) == 1


def test_expense_binding_failure_does_not_read_another_report():
    request = SimpleNamespace(post=lambda *a, **kw: pytest.fail('must not read order'))
    records = travel_invoices.read_invoices(request, [expense(bxdh='OTHER')], marker='SYNTHETIC-TRAVEL', origin='https://trip.example.test', timeout_sec=1)
    assert records[0]['reason'] == 'travel_order_expense_binding_invalid'


def test_order_invoice_keeps_oa_detail_links_and_rejects_conflicting_file_identity():
    payload = order()
    request = SimpleNamespace(post=lambda *a, **kw: SimpleNamespace(status=200, json=lambda: payload))
    records = travel_invoices.read_invoices(request, [expense()], marker='SYNTHETIC-TRAVEL', origin='https://trip.example.test', timeout_sec=1)
    result = TravelEvidence.assemble([CandidateObservation(text='SYNTHETIC-TRAVEL', scan_status='empty', order_invoices=tuple(records))],
        fields=[{'field_name': 'slvid', 'raw_value': 'detail-a', 'path': 'detail[4].row[0].field[9]'}], request_id='12', flow_mark='SYNTHETIC-TRAVEL', expected_resource_count=0)
    fact = json.loads(next(item['value'] for item in result.facts if item['name'] == '商旅订单发票'))
    assert fact['oa_detail_refs'] == ['detail[4].row[0]']
    second = json.loads(json.dumps(records[0]))
    second['invoices'][0]['invoiceNo'] = '00000000000000000002'
    with pytest.raises(ValueError, match='file_conflict'):
        travel_invoices.project(records + [second], [])


@pytest.mark.parametrize('declared_count', ['0', '1'])
def test_complete_collection_reads_original_text_and_cannot_hide_missing_manual_attachment(browser_transport, monkeypatch, tmp_path, declared_count):
    from zipfile import ZipFile
    from apps.oa.approvals.collection import case, downloads
    from tests.approvals.source_fakes import POLICY
    from tests.approvals.invoice_fakes import InvoiceApi

    config = {'flow_key': 'travel_expense', 'workflow_id': '138023', 'workflow_name': '差旅费报销流程'}
    api = InvoiceApi(config=config, rows=[[{'fieldName': 'slvid', 'fieldValue': 'detail-a'}]])
    workflow, _ = api.request_context('12')
    workflow['requestId'] = '12'
    workflow['workflowMainTableInfo'] = {'requestRecords': [{'workflowRequestTableFields': [
        {'fieldName': 'lcbh', 'fieldShowName': '流程编号', 'fieldShowValue': 'SYNTHETIC-TRAVEL'},
        {'fieldName': 'fjsl', 'fieldShowName': '附件数量', 'fieldShowValue': declared_count},
    ]}]}
    api.request_context = lambda _: (workflow, [])
    browser_transport.outcomes[True] = {'expense_rows': [expense()], 'hotel_payload': order()}
    original = tmp_path / 'invoice.ofd'
    with ZipFile(original, 'w') as archive:
        archive.writestr('Doc_0/Pages/Page_0/Content.xml', '<Page><TextCode>电子发票（铁路电子客票）</TextCode><TextCode>示例公司</TextCode><TextCode>SYNTHETIC-TAX</TextCode><TextCode>43.00</TextCode></Page>')
    calls = []

    def download(targets, **kwargs):
        calls.extend(targets)
        return downloads.DownloadResult(tuple({'resource_id': item['id'], 'path': str(original), 'name': item['name']} for item in targets), (), 'complete')

    monkeypatch.setattr(downloads, 'download_resources', download)
    if declared_count == '1':
        with pytest.raises(case.CollectionFailure, match='travel_attachment_resources_incomplete'):
            case.collect_case('12', oa_api=api, configs=[config], source_policy=POLICY, allow_browser_download=True, attachment_timeout_sec=1)
        assert calls == []
        return
    material = case.collect_case('12', oa_api=api, configs=[config], source_policy=POLICY, allow_browser_download=True, attachment_root=tmp_path/'attachments', prepared_root=tmp_path/'prepared')
    assert len(calls) == 1
    resource, = material['resources']
    prepared, = material['prepared']['structured_contents']
    assert prepared['source_resource_id'] == resource['resource_id']
    assert '示例公司' in prepared['key_text']
    assert 'SYNTHETIC-TAX' in prepared['key_text']
    fact = json.loads(next(item['value'] for item in material['authority_facts'] if item['name'] == '商旅订单发票'))
    assert fact['oa_detail_refs'] == ['detail[0].row[0]']
    assert fact['invoices'][0]['resource_id'] == resource['resource_id']


def test_missing_expense_source_is_not_treated_as_no_order_invoices(browser_transport):
    browser_transport.outcomes[True] = {'expense_rows': None}
    evidence = collect_travel(count='0')
    assert evidence.errors[0].code == 'travel_order_expenses_unavailable'


@pytest.mark.parametrize('status', [302, 403, 500])
def test_failed_order_read_preserves_technical_failure(status):
    request = SimpleNamespace(post=lambda *a, **kw: SimpleNamespace(status=status))
    records = travel_invoices.read_invoices(request, [expense()], marker='SYNTHETIC-TRAVEL', origin='https://trip.example.test', timeout_sec=1)
    assert records[0]['reason'] == 'travel_order_invoice_http_failed'
    assert records[0]['invoices'] == []
