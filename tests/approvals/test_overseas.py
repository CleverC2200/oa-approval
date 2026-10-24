
from tests.approvals.source_fakes import POLICY
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import urllib.parse

import fitz
import pytest

from apps.oa.approvals.collection import overseas
from apps.oa.approvals.operations import submit_overseas
from apps.oa.approvals.operations import Approval
from apps.oa.approvals import overseas as invoice
from apps.oa.approvals.sources import overseas as overseas_invoice_api


@pytest.fixture
def case(tmp_path):
    def build(groups=(('101', '102'),)):
        records, resources, files = [], [], []
        for order, ids in enumerate(groups, 1):
            fields = [{'fieldName': name, 'fieldValue': value, 'edit': True} for name, value in (
                ('scfp', ','.join(ids)), ('fpje', '7.00'), ('fphm', 'original-number'),
                ('fpdqr', '2026-11-01'), ('payment', 'unchanged'))]
            fields.extend([
                {'fieldName': 'custom_date', 'fieldShowName': '发票日期', 'fieldValue': '2026-10-01', 'edit': True},
                {'fieldName': 'custom_invoice_po', 'fieldShowName': '发票采购订单号', 'fieldValue': 'old-po', 'edit': True},
                {'fieldName': 'chosen_po', 'fieldShowName': '采购订单号', 'fieldValue': '321',
                 'fieldShowValue': '001234', 'fieldHtmlType': '3', 'edit': True}])
            fields[0]['filedHtmlShow'] = ''.join(
                f'<a href="/news/download.do?fileid={rid}&filename={rid}.pdf">invoice</a>' for rid in ids)
            records.append({'recordOrder': order, 'workflowRequestTableFields': fields})
            for rid in ids:
                path = tmp_path / (rid + '.pdf')
                with fitz.open() as pdf:
                    pdf.new_page().insert_text((20, 20), rid)
                    pdf.new_page().insert_text((20, 20), rid + ' page 2')
                    pdf.save(path)
                resources.append({'id': rid, 'name': path.name, 'downloadUrl': '/download/' + rid})
                files.append({'resource_id': rid, 'path': str(path), 'pages': 2})
        data = {'requestId': '201', 'workflowId': invoice.WORKFLOW_ID,
                'workflowName': invoice.WORKFLOW_NAME, 'currentNodeId': invoice.RPA_NODE_ID, 'canEdit': True,
                'workflowMainTableInfo': {'requestRecords': [{'workflowRequestTableFields': [
                    {'fieldName': 'bz', 'fieldValue': 'USD'}]}]},
                'workflowDetailTableInfos': [{'tableDBName': 'formtable_main_1295_dt1',
                                             'workflowRequestTableRecords': records}]}
        material = {'snapshot': invoice.snapshot(data, resources, origins=POLICY.attachments), 'attachments': [
            {'resource_id': item['resource_id'], 'sha256': hashlib.sha256(Path(item['path']).read_bytes()).hexdigest(),
             'pages': item['pages']} for item in files]}
        recognized = {'material_digest': invoice.digest(material), 'invoices': [
            reading(item['resource_id'], item['sha256'], str(index + 1) + '.10', 'INV-' + item['resource_id'])
            for index, item in enumerate(material['attachments'])]}
        return data, resources, files, material, recognized
    return build


def reading(resource_id, sha256, amount, number):
    def field(value):
        return {'status': 'recognized', 'value': value, 'page': 1, 'quote': str(value)}
    return {'resource_id': resource_id, 'sha256': sha256, 'currency': 'USD',
            'fpje': field(amount), 'fphm': field(number), 'fpdqr': field('2026-12-01'),
            'invoice_date': field('2026-10-20'), 'invoice_purchase_order': field('001234')}


def test_multiple_invoice_rows_sum_separately_and_preserve_human_selection(case):
    data, _, _, material, recognized = case((('101', '102'), ('103',)))
    plan = invoice.build_plan(material, recognized)
    assert plan['updates'] == [
        {'row_key': 'formtable_main_1295_dt1:1', 'values': {
            'fpje': '3.20', 'fphm': 'INV-101,INV-102', 'fpdqr': '2026-12-01',
            'custom_date': '2026-10-20', 'custom_invoice_po': '001234,001234'}},
        {'row_key': 'formtable_main_1295_dt1:2', 'values': {
            'fpje': '3.10', 'fphm': 'INV-103', 'fpdqr': '2026-12-01',
            'custom_date': '2026-10-20', 'custom_invoice_po': '001234'}}]
    patched = invoice.patched_workflow(data, material['snapshot'], plan)
    first = patched['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]
    assert {field['fieldName']: field['fieldValue'] for field in first['workflowRequestTableFields']} == {
        'scfp': '101,102', 'fpje': '3.20', 'fphm': 'INV-101,INV-102',
        'fpdqr': '2026-12-01', 'payment': 'unchanged', 'custom_date': '2026-10-20',
        'custom_invoice_po': '001234,001234', 'chosen_po': '321'}
    assert '❌' not in plan['remark']


def test_one_attachment_can_contain_multiple_invoices(case):
    _, _, _, material, recognized = case((('101',),))
    extra = deepcopy(recognized['invoices'][0])
    extra['fpje'].update(value='2.20', page=2, quote='2.20')
    extra['fphm'].update(value='INV-B', page=2, quote='INV-B')
    extra['invoice_date'].update(value='2026-10-21', page=2)
    extra['fpdqr'].update(value='2026-12-02', page=2)
    recognized['invoices'].insert(0, extra)
    assert invoice.build_plan(material, recognized)['updates'][0]['values'] == {
        'fpje': '3.30', 'fphm': 'INV-101,INV-B', 'fpdqr': '2026-12-01',
        'custom_date': '2026-10-20', 'custom_invoice_po': '001234,001234'}


def test_exact_duplicate_attachments_are_counted_once(case):
    _, _, _, material, recognized = case()
    material['attachments'][1]['sha256'] = material['attachments'][0]['sha256']
    recognized['material_digest'] = invoice.digest(material)
    recognized['invoices'][1] = {**deepcopy(recognized['invoices'][0]), 'resource_id': '102'}
    assert invoice.build_plan(material, recognized)['updates'][0]['values']['fpje'] == '1.10'


@pytest.mark.parametrize('field', ['fphm', 'fpdqr', 'invoice_date'])
def test_multi_invoice_optional_fields_can_stay_missing(case, field):
    _, _, _, material, recognized = case()
    recognized['invoices'][1][field].update(status='missing', value=None, quote='Not stated')
    values = invoice.build_plan(material, recognized)['updates'][0]['values']
    assert values['fpje'] == '3.20'
    assert values['fphm'] == ('INV-101' if field == 'fphm' else 'INV-101,INV-102')
    assert values['custom_date'] == '2026-10-20'
    assert values['fpdqr'] == '2026-12-01'


@pytest.mark.parametrize('problem,error', [
    ('missing_amount', 'invoice_aggregate_amount_missing'),
    ('currency', 'invoice_currency_mismatch'),
    ('coverage', 'invoice_recognition_incomplete'),
    ('same_number', 'invoice_number_conflict'),
    ('repeat_entry', 'invoice_recognition_duplicate'),
    ('duplicate_conflict', 'invoice_duplicate_conflict'),
    ('overflow', 'invoice_amount_invalid'),
])
def test_uncertain_multi_invoice_totals_are_rejected(case, problem, error):
    _, _, _, material, recognized = case()
    if problem == 'missing_amount':
        recognized['invoices'][1]['fpje'].update(status='missing', value=None, quote='No total shown')
    elif problem == 'currency':
        recognized['invoices'][1]['currency'] = 'EUR'
    elif problem == 'coverage':
        recognized['invoices'].pop()
    elif problem == 'same_number':
        recognized['invoices'][1]['fphm']['value'] = recognized['invoices'][0]['fphm']['value']
    elif problem == 'duplicate_conflict':
        material['attachments'][1]['sha256'] = material['attachments'][0]['sha256']
        recognized['invoices'][1]['sha256'] = material['attachments'][0]['sha256']
        recognized['material_digest'] = invoice.digest(material)
    elif problem == 'overflow':
        for part in recognized['invoices']:
            part['fpje']['value'] = '999999999999999999999999999999.99'
    else:
        recognized['invoices'].append(deepcopy(recognized['invoices'][0]))
    with pytest.raises(ValueError, match=error):
        invoice.build_plan(material, recognized)


def test_large_amounts_sum_without_decimal_context_rounding(case):
    _, _, _, material, recognized = case()
    recognized['invoices'][0]['fpje']['value'] = '999999999999999999999999999998.99'
    recognized['invoices'][1]['fpje']['value'] = '0.01'
    assert invoice.build_plan(material, recognized)['updates'][0]['values']['fpje'] == '999999999999999999999999999999.00'


def test_attachment_list_must_cover_every_declared_file(case):
    data, _, _, _, _ = case()
    data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields'][0]['fieldValue'] += ',103'
    with pytest.raises(ValueError, match='invoice_attachment_unbound'):
        invoice.snapshot(data, [], origins=POLICY.attachments)


def test_trusted_single_attachment_keeps_legacy_empty_raw_value(case):
    data, resources, _, _, _ = case((('101',),))
    data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields'][0]['fieldValue'] = ''
    assert invoice.snapshot(data, resources, origins=POLICY.attachments)['rows'][0]['resource_ids'] == ['101']


def test_attachment_binding_rejects_same_count_with_wrong_id(case):
    data, resources, _, _, _ = case()
    field = data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields'][0]
    field['fieldValue'] = '101,103'
    with pytest.raises(ValueError, match='invoice_attachment_unbound'):
        invoice.snapshot(data, resources, origins=POLICY.attachments)


def test_first_invoice_follows_declared_attachment_order(case):
    data, resources, _, material, recognized = case()
    field = data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields'][0]
    field['fieldValue'] = '102,101'
    material['snapshot'] = invoice.snapshot(data, resources, origins=POLICY.attachments)
    recognized['material_digest'] = invoice.digest(material)
    recognized['invoices'][1]['invoice_date']['value'] = '2026-10-22'
    recognized['invoices'][1]['fpdqr']['value'] = '2026-12-22'
    values = invoice.build_plan(material, recognized)['updates'][0]['values']
    assert values['custom_date'] == '2026-10-22'
    assert values['fpdqr'] == '2026-12-22'
    assert values['fphm'] == 'INV-102,INV-101'


@pytest.mark.parametrize('field,physical', [('invoice_date', 'custom_date'), ('fpdqr', 'fpdqr')])
def test_first_invoice_missing_date_keeps_original_without_fallback(case, field, physical):
    data, _, _, material, recognized = case()
    recognized['invoices'][0][field].update(status='missing', value=None, quote='Not stated')
    plan = invoice.build_plan(material, recognized)
    assert physical not in plan['updates'][0]['values']
    assert '首张发票未列明' + invoice.LABELS[field] in plan['remark']
    patched = invoice.patched_workflow(data, material['snapshot'], plan)
    record = patched['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]
    assert next(field['fieldValue'] for field in record['workflowRequestTableFields']
                if field['fieldName'] == physical) == ('2026-10-01' if physical == 'custom_date' else '2026-11-01')


@pytest.mark.parametrize('problem', ['missing', 'duplicate', 'not_editable'])
def test_exact_field_mapping_required_for_new_fields(case, problem):
    data, resources, _, _, _ = case()
    fields = data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields']
    target = next(field for field in fields if field['fieldName'] == 'custom_invoice_po')
    if problem == 'missing':
        fields.remove(target)
    elif problem == 'duplicate':
        fields.append({**target, 'fieldName': 'another_po'})
    else:
        target['edit'] = False
    with pytest.raises(ValueError, match='invoice_field'):
        invoice.snapshot(data, resources, origins=POLICY.attachments)


def test_unresolved_human_selection_is_reported_without_using_browser_id(case):
    data, resources, _, material, recognized = case()
    fields = data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields']
    next(field for field in fields if field['fieldName'] == 'chosen_po').pop('fieldShowValue')
    material['snapshot'] = invoice.snapshot(data, resources, origins=POLICY.attachments)
    recognized['material_digest'] = invoice.digest(material)
    assert '人工所选采购订单号未确认' in invoice.build_plan(material, recognized)['remark']


def test_submit_adapter_sends_mapped_invoice_fields_and_rejects_human_selection(case, monkeypatch):
    data, _, _, material, recognized = case()
    plan = invoice.build_plan(material, recognized)
    details = invoice.detail_data(data, material['snapshot'], plan)
    allowed = {('formtable_main_1295_dt1', '1'): set(material['snapshot']['rows'][0]['field_names'].values())}
    sent = []

    def send(request, **kwargs):
        sent.append(urllib.parse.parse_qs(request.data.decode()))
        return io.BytesIO(b'{"code":"SUCCESS"}')

    monkeypatch.setattr(overseas_invoice_api.safe_http, 'urlopen', send)
    api = overseas_invoice_api.InvoiceApi('fake-token', 'fake-user', app_id='fake-app', base_url='https://example.invalid')
    assert api.submit_invoice('201', plan['remark'], details, fields_by_row=allowed) == {'code': 'SUCCESS'}
    assert json.loads(sent[0]['detailData'][0]) == details
    details[0]['workflowRequestTableRecords'][0]['workflowRequestTableFields'].append(
        {'fieldName': 'chosen_po', 'fieldValue': '009999'})
    with pytest.raises(ValueError, match='invoice_field_not_allowed'):
        api.submit_invoice('201', plan['remark'], details, fields_by_row=allowed)
    assert len(sent) == 1


@pytest.mark.parametrize('tamper,po_problem,lost_response', [
    (False, None, False), (True, None, False), (False, 'missing', False), (False, 'mismatch', False),
    (False, None, True), (True, None, True)])
def test_multi_invoice_collect_preview_submit_and_postcheck(case, tmp_path, monkeypatch, tamper, po_problem, lost_response):
    data, resources, files, _, _ = case()
    pending = [{'request_id': '201', 'received_at': '2026-10-23 09:00:00',
                'operator_id': 'tester', 'workflow_key': 'overseas_invoice'}]
    posts, handled = [], []

    class Api:
        def pending_rows(self):
            return pending

        def handled_rows(self):
            return handled

        def request_context(self, request_id):
            assert request_id == '201'
            return data, resources

        def submit_invoice(self, request_id, remark, details, *, fields_by_row):
            assert fields_by_row == {('formtable_main_1295_dt1', '1'): {
                'fpje', 'fphm', 'fpdqr', 'custom_date', 'custom_invoice_po'}}
            if po_problem:
                assert '❌' in remark
                assert ('采购订单号为空' if po_problem == 'missing' else '与所选采购订单号001234不一致') in remark
            else:
                assert '❌' not in remark
            posts.append(details)
            fields = data['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields']
            changes = details[0]['workflowRequestTableRecords'][0]['workflowRequestTableFields']
            for change in changes:
                next(field for field in fields if field['fieldName'] == change['fieldName'])['fieldValue'] = change['fieldValue']
            if tamper:
                next(field for field in fields if field['fieldName'] == 'fphm')['fieldValue'] = 'unexpected-change'
            pending.clear()
            handled.append({'request_id': request_id, 'handled_at': datetime.now(timezone.utc).isoformat()})
            if lost_response:
                raise OSError('connection lost after applying invoice fields')
            return {'code': 'SUCCESS'}

    monkeypatch.setattr(overseas, 'download_fresh', lambda selected, *args, **kwargs: files)
    app = Approval(tmp_path / 'runtime', api=Api(), configs=[], source_policy=POLICY)
    task = app.collect(app.pending()['run_id'], agent_id='agent-a', expected_task_id=1, agent_workflow_key='overseas_invoice')
    material = json.loads(Path(task['material_file']).read_text())
    recognized = {'material_digest': invoice.digest(material), 'invoices': [
        reading(part['resource_id'], part['sha256'], '10.10', 'INV-' + part['resource_id'])
        for part in material['attachments']]}
    if po_problem == 'missing':
        recognized['invoices'][1]['invoice_purchase_order'].update(status='missing', value=None, quote='Not stated')
    elif po_problem == 'mismatch':
        recognized['invoices'][1]['invoice_purchase_order']['value'] = '009999'
    assert len(material['attachments']) == 2
    preview = app.submit(task['task_id'], recognition=recognized)
    assert preview['status'] == 'preview'
    assert preview['field_plan']['updates'] == [{'row_key': 'formtable_main_1295_dt1:1', 'values': {
        'fpje': '20.20', 'fphm': 'INV-101,INV-102', 'fpdqr': '2026-12-01',
        'custom_date': '2026-10-20', 'custom_invoice_po': {
            None: '001234,001234', 'missing': '001234', 'mismatch': '001234,009999'}[po_problem]}}]
    assert preview['field_plan']['remark'] == preview['opinion']
    assert posts == []
    with pytest.raises(ValueError, match='overseas_authorization_required'):
        app.submit(task['task_id'], recognition=recognized, confirm_write=True)
    result = app.submit(task['task_id'], recognition=recognized, confirm_write=True, allow_overseas=True)
    expected = 'unknown' if tamper else 'submitted'
    assert result['status'] == ('unknown' if lost_response else expected)
    if not lost_response:
        assert result['fields_match'] is not tamper
    checked = app.postcheck(task['task_id'])
    assert checked['status'] == expected
    if lost_response or tamper:
        assert checked['fields_match'] is not tamper
        assert checked['submit_ok'] is True
        assert checked['postcheck_done_hit'] is not tamper
    assert app.submit(task['task_id'], recognition=recognized, confirm_write=True, allow_overseas=True)['status'] == expected
    assert len(posts) == 1
    assert {item['fieldName']: item['fieldValue'] for item in
            posts[0][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields']} == {
        'fpje': '20.20', 'fphm': 'INV-101,INV-102', 'fpdqr': '2026-12-01',
        'custom_date': '2026-10-20', 'custom_invoice_po': {
            None: '001234,001234', 'missing': '001234', 'mismatch': '001234,009999'}[po_problem]}


@pytest.mark.parametrize('response,pending,fresh,expected_code', [
    ('ERROR', False, True, 'writer_submit_rejected'),
    ('UNRECOGNIZED', False, True, 'writer_result_unknown'),
    ('SUCCESS', True, True, 'writer_postcheck_timeout'),
    ('SUCCESS', False, False, 'writer_postcheck_timeout'),
    ('SUCCESS', False, True, None),
])
def test_overseas_submit_preserves_response_and_completion_evidence(case, response, pending, fresh, expected_code):
    data, resources, _, material, recognized = case()
    plan = invoice.build_plan(material, recognized)
    posts = []
    class Api:
        def request_context(self, request_id):
            return data, resources

        def submit_invoice(self, request_id, remark, details, *, fields_by_row):
            posts.append(details)
            data.update(invoice.patched_workflow(data, material['snapshot'], plan))
            return {'code': response}

        def pending_rows(self):
            return [{'request_id': '201'}] if pending else []

        def handled_rows(self):
            return [{'request_id': '201', 'handled_at': datetime.now(timezone.utc).isoformat()
                     if fresh else '2020-01-01T12:00:00+00:00'}]

    api = Api()
    result = submit_overseas(api, api, '201', material, plan, origins=POLICY.attachments)
    assert result['failure_code'] == expected_code
    assert result['submit_ok'] is (response == 'SUCCESS')
    assert result['done_hit'] is (response == 'SUCCESS' and fresh)
    assert result['postcheck_done_hit'] is (expected_code is None)
    assert len(posts) == 1


def test_missing_injected_overseas_writer_never_builds_a_real_adapter(case, tmp_path, monkeypatch):
    data, resources, files, _, _ = case()
    class Api:
        def pending_rows(self):
            return [{'request_id': '201', 'received_at': '2026-10-23 09:00:00',
                     'operator_id': 'tester', 'workflow_key': 'overseas_invoice'}]

        def request_context(self, request_id):
            return data, resources

    monkeypatch.setattr(overseas, 'download_fresh', lambda selected, *args, **kwargs: files)
    monkeypatch.setattr(overseas_invoice_api, 'build_api', lambda: pytest.fail('injected adapter must not fall back to real credentials'))
    app = Approval(tmp_path / 'runtime', api=Api(), writer=object(), configs=[], source_policy=POLICY)
    started = app.pending()
    task = app.collect(started['run_id'], agent_id='reviewer', expected_task_id=1, agent_workflow_key='overseas_invoice')
    material = json.loads(Path(task['material_file']).read_text())
    recognition = {'material_digest': invoice.digest(material), 'invoices': [
        reading(item['resource_id'], item['sha256'], '10.10', 'INV-' + item['resource_id'])
        for item in material['attachments']]}
    with pytest.raises(ValueError, match='invoice_write_adapter_required'):
        submit_overseas(app.api, app.writer, '201', material, invoice.build_plan(material, recognition), origins=POLICY.attachments)
    result = app.submit(task['task_id'], recognition=recognition, confirm_write=True, allow_overseas=True)
    assert result['status'] == 'unknown'
    assert len(app.inspect(task['task_id'])['submissions']) == 1


def test_default_overseas_write_uses_one_fresh_adapter_for_preflight_and_verification(case, tmp_path, monkeypatch):
    from apps.oa.approvals.sources import oa

    data, resources, files, _, _ = case()
    live = {'token': 0, 'pending': True}
    posts = []

    class ReadApi:
        def __init__(self, token):
            self.token = token

        def require_current_token(self):
            if self.token != live['token']:
                raise ValueError('token_expired')

        def pending_rows(self):
            self.require_current_token()
            return [{'request_id': '201', 'received_at': '2026-10-23 09:00:00',
                     'operator_id': 'tester', 'workflow_key': 'overseas_invoice'}] if live['pending'] else []

        def request_context(self, request_id):
            self.require_current_token()
            return data, resources

        def handled_rows(self):
            self.require_current_token()
            return [{'request_id': '201', 'handled_at': datetime.now(timezone.utc).isoformat()}] if posts else []

    class InvoiceApi(ReadApi):
        def submit_invoice(self, request_id, remark, details, *, fields_by_row):
            self.require_current_token()
            posts.append(details)
            for table in details:
                records = next(item for item in data['workflowDetailTableInfos']
                               if item['tableDBName'] == table['tableDBName'])['workflowRequestTableRecords']
                for update in table['workflowRequestTableRecords']:
                    fields = next(item for item in records if str(item['recordOrder']) == str(update['recordOrder']))['workflowRequestTableFields']
                    for value in update['workflowRequestTableFields']:
                        next(item for item in fields if item['fieldName'] == value['fieldName'])['fieldValue'] = value['fieldValue']
            live['pending'] = False
            return {'code': 'SUCCESS'}

    def fresh(client_type):
        live['token'] += 1
        return client_type(live['token'])

    monkeypatch.setattr(oa, 'build_api', lambda config, **kwargs: fresh(ReadApi))
    monkeypatch.setattr(overseas_invoice_api, 'build_api', lambda **kwargs: fresh(InvoiceApi))
    monkeypatch.setattr(overseas, 'download_fresh', lambda selected, *args, **kwargs: files)
    app = Approval(tmp_path / 'runtime', configs=[{}], source_policy=POLICY)
    task = app.collect(app.pending()['run_id'], agent_id='reviewer', expected_task_id=1, agent_workflow_key='overseas_invoice')
    material = json.loads(Path(task['material_file']).read_text())
    recognition = {'material_digest': invoice.digest(material), 'invoices': [
        reading(item['resource_id'], item['sha256'], '10.10', 'INV-' + item['resource_id'])
        for item in material['attachments']]}
    result = app.submit(task['task_id'], recognition=recognition, confirm_write=True, allow_overseas=True)
    assert result['status'] == 'submitted'
    assert result['fields_match'] is True
    assert len(posts) == 1
