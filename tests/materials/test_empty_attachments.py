from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa.materials import recognize as attachment_recognition
from apps.oa.materials import cli as materials_cli
from apps.oa.approvals.sources import official_sql
from apps.oa.approvals.sources import travel as travel_orders
from apps.oa.approvals.collection.case import collect_case


CONFIG = {'flow_key': 'general_expense', 'workflow_id': '195021',
          'workflow_name': '通用费用报销申请流程'}
TRAVEL_CONFIG = {'flow_key': 'travel_expense', 'workflow_id': '138023',
                 'workflow_name': '差旅费报销流程'}


@pytest.fixture
def travel_source(monkeypatch):
    workflow = json.loads((Path(__file__).parent / 'fixtures' / 'travel_empty_placeholders.json').read_text())
    scan = {'text': 'SYNTHETIC-TRAVEL 暂无数据', 'resources': [], 'attachment_scan_status': 'empty'}

    class TravelOaApi:
        def request_context(self, request_id):
            return workflow, []

    monkeypatch.setattr(travel_orders, '_ensure_cdp_available', lambda: True)
    monkeypatch.setattr(travel_orders, '_read_trip_candidate_results', lambda *_a, **_k: [scan])
    return workflow, scan, TravelOaApi()


class EmptyOaApi:
    def request_context(self, request_id):
        return {'requestId': request_id, 'workflowBaseInfo': {
            'workflowId': CONFIG['workflow_id'], 'workflowName': CONFIG['workflow_name'],
        }, 'workflowMainTableInfo': {'tableDBName': 'formtable_main_569'},
            'workflowDetailTableInfos': [{'tableDBName': 'formtable_main_569_dt1',
                'workflowRequestTableRecords': [{'recordOrder': '7', 'workflowRequestTableFields': [
                    {'fieldName': code, 'fieldShowName': name, 'fieldValue': '',
                     'fieldShowValue': '[该字段暂不支持]'}
                    for code, name in [('scfj', '上传附件'), ('fj', '附件'), ('glfp', '关联发票')]
                ]}]}]}, []


def test_collection_reuses_confirmed_official_empty_fields(monkeypatch):
    monkeypatch.setattr(official_sql, '_query_rows', lambda *_a, **_k: [{
        'requestid': '12', 'detail_id': '7', 'scfj': None, 'fj': None, 'glfp': None,
    }])
    material = collect_case('12', oa_api=EmptyOaApi(), configs=[CONFIG], allow_browser_download=False)

    assert material['attachment_presence'] == {
        'status': 'confirmed_empty', 'evidence_refs': [
            'material:declared_fields[0].empty_source',
            'material:declared_fields[1].empty_source',
            'material:declared_fields[2].empty_source',
        ],
    }
    assert [field['read_status'] for field in material['declared_fields']] == ['empty'] * 3


@pytest.mark.parametrize('entry', ['api', 'cli'])
def test_recognizer_skips_official_empty_material_without_model(tmp_path, monkeypatch, capsys, entry):
    monkeypatch.setattr(official_sql, '_query_rows', lambda *_a, **_k: [{
        'requestid': '12', 'detail_id': '7', 'scfj': None, 'fj': None, 'glfp': None,
    }])
    material = collect_case('12', oa_api=EmptyOaApi(), configs=[CONFIG], allow_browser_download=False)
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    original = material_file.read_bytes()
    requests = []
    monkeypatch.setattr(attachment_recognition, '_post', lambda *a, **k: requests.append(a))

    if entry == 'api':
        report = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')
    else:
        assert materials_cli.main(['recognize', 
            '--material-file', str(material_file), '--output-dir', str(tmp_path / 'recognized')]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary['status'] == 'skipped'
        assert summary['reason'] == 'official_no_attachments'
        report = json.loads(Path(summary['report_file']).read_text())

    assert report['status'] == 'skipped'
    assert report['reason'] == 'official_no_attachments'
    assert report['coverage'] == 'no_attachments'
    assert report['attachment_presence'] == material['attachment_presence']
    assert report['parts'] == []
    assert requests == []
    assert material_file.read_bytes() == original
    assert json.loads(Path(report['report_file']).read_text()) == report


def test_travel_collection_uses_zero_count_only_after_source_read(monkeypatch):
    from apps.oa.approvals.sources import travel as travel_orders

    config = {'flow_key': 'travel_expense', 'workflow_id': '138023', 'workflow_name': '差旅费报销流程'}

    class TravelOaApi:
        def request_context(self, request_id):
            return {'requestId': request_id, 'workflowBaseInfo': {
                'workflowId': config['workflow_id'], 'workflowName': config['workflow_name'],
            }, 'workflowMainTableInfo': {'requestRecords': [{'workflowRequestTableFields': [
                {'fieldName': 'fjsl', 'fieldShowName': '附件数量', 'fieldShowValue': '0'},
                {'fieldName': 'lcbh', 'fieldShowName': '流程编号', 'fieldShowValue': 'SYNTHETIC-TRAVEL'},
            ]}]}}, []

    monkeypatch.setattr(travel_orders, '_ensure_cdp_available', lambda: True)
    monkeypatch.setattr(travel_orders, '_read_trip_candidate_results', lambda *_a, **_k: [{
        'text': 'SYNTHETIC-TRAVEL', 'resources': [], 'attachment_scan_status': 'empty',
    }])
    material = collect_case('12', oa_api=TravelOaApi(), configs=[config], allow_browser_download=True)
    assert material['attachment_presence'] == {
        'status': 'confirmed_empty', 'evidence_refs': [
            'material:declared_fields[0]', 'material:attachment_scan'],
    }

    monkeypatch.setattr(travel_orders, '_read_trip_candidate_results', lambda *_a, **_k: [{
        'text': 'SYNTHETIC-TRAVEL', 'resources': [], 'attachment_scan_status': 'read_failed',
    }])
    with pytest.raises(RuntimeError, match='travel_attachment_discovery_failed'):
        collect_case('12', oa_api=TravelOaApi(), configs=[config], allow_browser_download=True)


@pytest.mark.parametrize('entry', ['api', 'cli'])
def test_travel_placeholder_fields_skip_only_with_official_empty_scan(
        tmp_path, monkeypatch, capsys, travel_source, entry):
    workflow, scan, oa_api = travel_source
    material = collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    original = material_file.read_bytes()
    monkeypatch.setattr(attachment_recognition, '_post', lambda *_a, **_k: pytest.fail('no model request'))

    if entry == 'api':
        report = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')
    else:
        assert materials_cli.main(['recognize', 
            '--material-file', str(material_file), '--output-dir', str(tmp_path / 'recognized')]) == 0
        report = json.loads(Path(json.loads(capsys.readouterr().out)['report_file']).read_text())

    assert report['status'] == 'skipped'
    assert report['reason'] == 'official_no_attachments'
    assert report['coverage'] == 'no_attachments'
    assert material['attachment_scan'] == {
        'status': 'empty', 'source': 'travel_platform', 'source_request_id': '12',
        'discovery_ref': 'travel_platform:SYNTHETIC-TRAVEL',
    }
    assert material['attachment_presence']['evidence_refs'] == [
        'material:declared_fields[2]', 'material:attachment_scan']
    assert [field['read_status'] for field in material['declared_fields'] if field['code'] == 'fj'] == [
        'empty', 'unresolved', 'unresolved']
    assert material_file.read_bytes() == original


def test_travel_empty_scan_preserves_placeholder_inputs(travel_source):
    workflow, scan, oa_api = travel_source
    fields = workflow['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0][
        'workflowRequestTableFields']
    fields[0]['filedHtmlShow'] = '<span style="color:red">[该字段暂不支持]</span>'
    fields[1]['fieldValue'] = '[该字段仅支持显示]'
    fields[1]['filedHtmlShow'] = '[该字段仅支持显示]'

    material = collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)

    assert material['attachment_presence']['status'] == 'confirmed_empty'
    declared = {field['code']: field for field in material['declared_fields']
                if field['path'].startswith('detail[0].row[0]')}
    assert declared['fj']['raw_value'] == ''
    assert declared['fj']['attachment_html'] == '<span style="color:red">[该字段暂不支持]</span>'
    assert declared['fphm']['raw_value'] == '[该字段仅支持显示]'
    assert declared['fphm']['attachment_html'] == '[该字段仅支持显示]'
    assert declared['fphm']['read_status'] == 'unresolved'


@pytest.mark.parametrize('address_value,extra_html,expected', [
    ('', '', 'skipped'), ('receipt.pdf', '', 'failed'),
    ('', '<a href="/synthetic.pdf">receipt.pdf</a>', 'failed'),
])
def test_zero_downloads_preserves_trip_and_hotel_page_review(
        tmp_path, monkeypatch, travel_source, address_value, extra_html, expected):
    workflow, scan, oa_api = travel_source
    fields = workflow['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0][
        'workflowRequestTableFields']
    fields[2]['filedHtmlShow'] = (
        '<table style="width:100%;"><tr><td align="left" style="width:99%;">'
        f'<input type="text" name="field_address" id="field_address" value="{address_value}" '
        f'onchange="checkinput(this)">{extra_html}</td></tr></table>')
    scan['text'] = 'SYNTHETIC-TRAVEL 行程：上海至杭州；住宿：合成酒店，入住2026-10-20，离店2026-10-21'
    fields.append({'fieldName': 'fysm', 'fieldShowName': '费用说明', 'fieldShowValue': '合成酒店一晚'})
    hotel_detail = {'order_no': 'synthetic-hotel', 'expense_description': '合成酒店一晚',
                    'hotel_name': '合成酒店', 'hotel_address': '浙江省杭州市西湖区合成路1号',
                    'city': '杭州市', 'district': '西湖区', 'check_in': '2026-10-20',
                    'check_out': '2026-10-21', 'order_detail': {'ddbh': 'synthetic-hotel', 'ddztmc': '已完成'}}
    scan['hotel_order_details'] = [hotel_detail]
    second_page = 'SYNTHETIC-TRAVEL 住宿结算详情：合成退款记录'
    monkeypatch.setattr(travel_orders, '_read_trip_candidate_results', lambda *_a, **_k: [
        scan, {**scan, 'text': second_page}])
    material = collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)
    page = next(fact for fact in material['authority_facts'] if fact['name'] == '商旅页面内容')
    assert page['value'] == scan['text']
    assert {fact['value'] for fact in material['authority_facts'] if fact['name'] == '商旅页面内容'} == {
        scan['text'], second_page}
    detail = next(fact for fact in material['authority_facts'] if fact['name'] == '商旅住宿订单详情')
    assert json.loads(detail['value']) == hotel_detail
    assert detail['subject_ref'] == 'detail[0].row[0]'
    address = next(fact for fact in material['authority_facts'] if fact['name'] == '酒店地址')
    assert address['value'] == hotel_detail['hotel_address']
    assert address['subject_ref'] == detail['subject_ref']
    assert material['resources'] == []
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    monkeypatch.setattr(attachment_recognition, '_post', lambda *_a, **_k: pytest.fail('no model request'))
    report = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')
    assert report['status'] == expected
    assert json.loads(material_file.read_text())['authority_facts'] == material['authority_facts']
    if expected == 'skipped':
        assert report['reason'] == 'official_no_attachments'
        assert material['attachment_presence']['status'] == 'confirmed_empty'
    else:
        assert 'attachment_presence' not in material
        assert report['error_code'] == 'attachment_content_empty'


@pytest.mark.parametrize('failure', ['read_failed', 'wrong_identity', 'browser_unavailable', 'source_exception'])
def test_travel_placeholder_source_failure_never_becomes_empty(travel_source, monkeypatch, failure):
    workflow, scan, oa_api = travel_source
    if failure == 'read_failed':
        scan['attachment_scan_status'] = 'read_failed'
    elif failure == 'wrong_identity':
        scan['text'] = 'SYNTHETIC-OTHER-REQUEST 暂无数据'
    elif failure == 'browser_unavailable':
        monkeypatch.setattr(travel_orders, '_ensure_cdp_available', lambda: False)
    else:
        def fail_read(*_a, **_k):
            raise RuntimeError('synthetic_source_failed')

        monkeypatch.setattr(travel_orders, '_read_trip_candidate_results', fail_read)

    with pytest.raises(RuntimeError, match='travel_attachment_discovery_failed'):
        collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)


@pytest.mark.parametrize('positive', [
    'raw_attachment', 'display_attachment', 'attachment_html', 'invoice_html',
    'invoice_number', 'linked_invoice',
])
def test_travel_zero_count_and_empty_scan_do_not_hide_declared_content(
        tmp_path, monkeypatch, travel_source, positive):
    workflow, scan, oa_api = travel_source
    field = workflow['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0][
        'workflowRequestTableFields'][0]
    if positive == 'raw_attachment':
        field['fieldValue'] = '999'
    elif positive == 'display_attachment':
        field['fieldShowValue'] = 'synthetic.pdf'
    elif positive == 'attachment_html':
        field['filedHtmlShow'] = '<span>synthetic.pdf</span>'
    elif positive == 'invoice_html':
        field.update(fieldName='fphm', fieldShowName='发票号码',
                     filedHtmlShow='<span>synthetic invoice</span>')
    elif positive == 'invoice_number':
        field.update(fieldName='fphm', fieldShowName='发票号码',
                     fieldShowValue='12345678901234567890')
    else:
        field.update(fieldName='glfp', fieldShowName='关联发票', fieldValue='999')

    if positive in {'linked_invoice', 'invoice_number'}:
        with pytest.raises(RuntimeError, match='invoice_buyer_read_failed'):
            collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)
        return

    material = collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)
    assert 'attachment_presence' not in material
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    monkeypatch.setattr(attachment_recognition, '_post', lambda *_a, **_k: pytest.fail('no model request'))
    report = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')
    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_content_empty'


def test_travel_positive_count_still_requires_resources(travel_source):
    workflow, scan, oa_api = travel_source
    workflow['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields'][2][
        'fieldShowValue'] = '1'

    with pytest.raises(RuntimeError, match='travel_attachment_resources_incomplete'):
        collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)


@pytest.mark.parametrize('failure', ['', 'missing_record', 'missing_file', 'duplicate_source', 'cloud_originals'])
def test_travel_count_uses_native_records_and_retains_each_source_identity(
        tmp_path, monkeypatch, travel_source, failure):
    from apps.oa.approvals.collection import downloads as attachments
    from apps.oa.approvals.sources import downloads as remote
    from apps.oa.approvals import forms as evidence_packet
    from apps.oa.approvals.collection import invoice_originals
    from apps.oa.approvals.collection.case import CollectionFailure

    workflow, scan, oa_api = travel_source
    workflow['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields'][2][
        'fieldShowValue'] = '6'
    monkeypatch.setattr(travel_orders, 'TRIP_BASE_URL', 'https://trip.example.test')
    monkeypatch.setattr(remote, 'EXTRA_DOWNLOAD_HOSTS', {'trip.example.test'})
    inventory = []
    rows = [{'id': f'expense-{i}', 'fjList': [
        {'id': f'attachment-{i}', 'fjdz': f'{100+i}/file.txt' if i < 4 else '',
         'fjmc': 'file.txt' if i < 4 else '', 'fjlx': ''}]} for i in range(6)]
    resources = travel_orders._attachment_resources_from_api_payload({'result': rows}, inventory)
    scan.update(resources=resources, attachment_scan_status='resources_found', attachment_inventory=inventory)
    monkeypatch.setattr(evidence_packet, 'logical_attachment_resources', lambda *_: [resources[0]])
    if failure == 'missing_record':
        inventory.pop()
    elif failure == 'missing_file':
        scan['resources'] = resources[:-1]
    elif failure == 'duplicate_source':
        scan['attachment_inventory'] = []
    elif failure == 'cloud_originals':
        scan['attachment_inventory'] = []
        monkeypatch.setattr(invoice_originals, 'resources', lambda *_, **__: [
            {'id': f'invoice-{i}', '_invoice_original': {}} for i in range(8)])

    def download(targets, **kwargs):
        import fitz

        result = []
        for target in targets:
            path = tmp_path / (target['id'] + '.pdf')
            with fitz.open() as document:
                document.new_page().insert_text((40, 40), 'synthetic attachment')
                document.save(path)
            result.append({'resource_id': target['id'], 'path': str(path), 'name': path.name})
        return result, []

    monkeypatch.setattr(attachments, 'download_resources', download)
    if failure:
        with pytest.raises(CollectionFailure, match='^travel_attachment_resources_incomplete$') as caught:
            collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)
        assert caught.value.counts['unique_source_resource_count'] <= 4
        assert caught.value.diagnostics[-1]['reason'] == 'source_inventory_incomplete'
    else:
        material = collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True,
                                attachment_root=tmp_path, prepared_root=tmp_path / 'prepared')
        assert len(material['resources']) == 4
        assert len(material['attachment_scan']['inventory']) == 6
        assert len(material['prepared']['manifest']) == 4


@pytest.mark.parametrize('missing', ['blank', 'absent'])
def test_travel_missing_count_uses_complete_empty_scan_without_inventing_zero(
        tmp_path, monkeypatch, travel_source, missing):
    workflow, scan, oa_api = travel_source
    fields = workflow['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields']
    if missing == 'blank':
        fields[2]['fieldShowValue'] = ''
    else:
        del fields[2]
    material = collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)

    assert material['attachment_presence'] == {
        'status': 'confirmed_empty', 'evidence_refs': ['material:attachment_scan']}
    assert [field['value'] for field in material['declared_fields'] if field['name'] == '附件数量'] == (
        [''] if missing == 'blank' else [])
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    monkeypatch.setattr(attachment_recognition, '_post', lambda *_a, **_k: pytest.fail('no model request'))
    report = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')
    assert report['status'] == 'skipped'
    assert report['reason'] == 'official_no_attachments'


def test_travel_missing_count_still_requires_complete_source_read(travel_source):
    workflow, scan, oa_api = travel_source
    workflow['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields'][2][
        'fieldShowValue'] = ''
    scan['attachment_scan_status'] = 'read_failed'

    with pytest.raises(RuntimeError, match='travel_attachment_discovery_failed'):
        collect_case('12', oa_api=oa_api, configs=[TRAVEL_CONFIG], allow_browser_download=True)


def test_zero_count_does_not_hide_unresolved_declared_attachment(monkeypatch):
    class DeclaredAttachmentApi(EmptyOaApi):
        def request_context(self, request_id):
            workflow, resources = super().request_context(request_id)
            workflow['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0][
                'workflowRequestTableFields'][0]['fieldValue'] = '999'
            workflow['workflowMainTableInfo']['requestRecords'] = [{'workflowRequestTableFields': [
                {'fieldName': 'fjsl', 'fieldShowName': '附件数量', 'fieldShowValue': '0'},
            ]}]
            return workflow, resources

    monkeypatch.setattr(official_sql, '_query_rows', lambda *_a, **_k: pytest.fail('no extra SQL lookup'))
    material = collect_case('12', oa_api=DeclaredAttachmentApi(), configs=[CONFIG], allow_browser_download=False)
    assert 'attachment_presence' not in material


@pytest.mark.parametrize('failure', ['read_failed', 'positive_count'])
def test_collection_does_not_confirm_unknown_empty_sources(monkeypatch, failure):
    class UnknownOaApi(EmptyOaApi):
        def request_context(self, request_id):
            workflow, resources = super().request_context(request_id)
            if failure == 'positive_count':
                workflow['workflowMainTableInfo']['requestRecords'] = [{'workflowRequestTableFields': [
                    {'fieldName': 'fjsl', 'fieldShowName': '附件数量', 'fieldShowValue': '1'},
                ]}]
            return workflow, resources

    def query(*args, **kwargs):
        if failure == 'read_failed':
            raise RuntimeError('official_sql_read_failed')
        return [{'requestid': '12', 'detail_id': '7', 'scfj': None, 'fj': None, 'glfp': None}]

    monkeypatch.setattr(official_sql, '_query_rows', query)
    material = collect_case('12', oa_api=UnknownOaApi(), configs=[CONFIG], allow_browser_download=False)
    assert 'attachment_presence' not in material


@pytest.mark.parametrize('change, expected', [
    ({}, 'attachment_content_empty'),
    ({'attachment_presence': {'status': 'confirmed_empty', 'evidence_refs': []}}, 'attachment_content_incomplete'),
    ({'attachment_presence': {'status': 'confirmed_empty', 'evidence_refs': ['material:declared_fields[0]']},
      'resources': [{'resource_id': 'missing'}]}, 'attachment_content_incomplete'),
    ({'attachment_presence': {'status': 'confirmed_empty', 'evidence_refs': ['material:declared_fields[0]']},
      'prepared': {'images': [], 'manifest': [{'status': 'failed'}], 'structured_contents': []}},
     'attachment_content_incomplete'),
    ({'attachment_presence': {'status': 'confirmed_empty', 'evidence_refs': ['material:declared_fields[0]']},
      'prepared': {'images': [], 'manifest': []}}, 'attachment_content_incomplete'),
])
def test_empty_lists_cannot_hide_unknown_or_failed_material(tmp_path, monkeypatch, change, expected):
    material = {'request_id': '12', 'resources': [],
                'prepared': {'images': [], 'manifest': [], 'structured_contents': []}, **change}
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    requests = []
    monkeypatch.setattr(attachment_recognition, '_post', lambda *a, **k: requests.append(a))

    report = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')

    assert report['status'] == 'failed'
    assert report['error_code'] == expected
    assert requests == []


@pytest.mark.parametrize(('selection', 'coverage'), [
    ({'resource_id': 'missing'}, 'selected_resource'),
    ({'resource_id': 'missing', 'page': 1}, 'selected_page'),
    ({'resource_id': 'missing', 'page': 1, 'region': (0, 0, 1, 1)}, 'image_region'),
])
def test_official_empty_material_does_not_hide_missing_selected_attachment(
        tmp_path, monkeypatch, selection, coverage):
    monkeypatch.setattr(official_sql, '_query_rows', lambda *_a, **_k: [{
        'requestid': '12', 'detail_id': '7', 'scfj': None, 'fj': None, 'glfp': None,
    }])
    material = collect_case('12', oa_api=EmptyOaApi(), configs=[CONFIG], allow_browser_download=False)
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    requests = []
    monkeypatch.setattr(attachment_recognition, '_post', lambda *a, **k: requests.append(a))

    report = attachment_recognition.recognize(
        material_file=material_file, output_dir=tmp_path / 'selected', **selection)

    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_resource_not_found'
    assert report['coverage'] == coverage
    assert report['selection']['resource_id'] == 'missing'
    assert json.loads(Path(report['observations_file']).read_text()) == []
    assert report['parts'] == requests == []
