from __future__ import annotations

from apps.oa.approvals.sources.travel_evidence import TravelEvidence

from tests.approvals.source_fakes import POLICY

from io import BytesIO
from pathlib import Path

from copy import deepcopy

import json

from types import SimpleNamespace

from urllib.parse import parse_qs, urlsplit

import fitz

import pytest

from apps.oa.approvals.sources import oa as api
from apps.oa.approvals.collection import downloads as attachments
from apps.oa.approvals.sources import downloads as remote
from apps.oa.approvals.sources import originals as original_source
from apps.oa.approvals.sources import auth
from apps.oa.approvals import flows as flow
from apps.oa.approvals.sources import invoices as invoice_buyers
from apps.oa.approvals.sources import official_sql

from apps.oa.approvals.collection.case import collect_case
from tests.approvals.invoice_fakes import CONFIG, NUMBER, InvoiceApi

@pytest.fixture
def original_transport(monkeypatch, tmp_path):
    client = InvoiceApi()
    workflow, _ = client.request_context('101')
    workflow['requestId'] = '101'
    client.request_context = lambda _request_id: (workflow, [])
    original_get = client.get
    client.get = lambda path, params=None, *, retry=True: (
        {'status': '1', 'targetIds': '9001'} if path == invoice_buyers.MAPPING_PATH
        else original_get(path, params)
    )
    document = fitz.open()
    page = document.new_page()
    page.insert_text((40, 40), 'Original invoice ' + NUMBER)
    state = SimpleNamespace(
        client=client, workflow=workflow, initialized=False, requests=[], actor='77',
        main_file='202', still_denied=False, pdf=document.tobytes(), cloud_pdf=None,
        after_cloud_download=None, cloud_failure=None, cloud_headers={}, content_type='application/pdf',
        detail={'id': '9001', 'fid': '9001', 'number': NUMBER,
                'flist': [{'id': '8001', 'type': 'pdf', 'size': 0, 'del': 0}],
                'modify_info': {'pdf': '8001', 'furl': '8001'},
                'oa': {'doc_id': '303', 'img_id': '202'}},
    )
    document.close()
    monkeypatch.setattr(api, 'build_api', lambda _config, **kwargs: client)
    monkeypatch.setattr(flow, 'load_all_configs', lambda: [CONFIG])
    monkeypatch.setattr(official_sql, 'workflow_attachment_document', lambda *_args: '')
    monkeypatch.setattr(remote.DownloadTransfer, '_rpa_cookie_header', staticmethod(lambda *_a, **_kw: 'session=fixed'))
    monkeypatch.setattr(auth, 'env', lambda key, default='': '77' if key == 'OA_UID' else default)
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CACHE_DB', str(tmp_path / 'cache.sqlite3'))

    class Response(BytesIO):
        def __init__(self, body, url):
            super().__init__(body)
            self.url = url
            self.headers = {'Content-Type': state.content_type, 'Content-Length': str(len(body))}

        def geturl(self):
            return self.url

    def open_request(request, **_kwargs):
        path = urlsplit(request.full_url).path
        params = parse_qs(urlsplit(request.full_url).query)
        state.requests.append((path, params, request.get_header('Cookie')))
        if request.full_url == invoice_buyers.DETAIL_URL:
            body = {'actionMsg': {'code': '0'}, 'data': state.detail}
        elif path == '/invoiceApi/file/download':
            assert urlsplit(request.full_url).hostname == 'api.mypiaojia.com'
            assert params == {'id': [state.detail['modify_info'].get('pdf') or state.detail['modify_info']['furl']], 'fid': ['9001'], 'dataId': ['101'],
                              'token': ['synthetic-token']}
            assert request.get_header('Cookie') is None
            if state.cloud_failure == 'http':
                from urllib.error import HTTPError
                raise HTTPError(request.full_url, 403, 'forbidden', {}, None)
            if state.cloud_failure == 'redirect':
                from apps.oa.approvals.sources.originals import _NoCloudRedirect
                return _NoCloudRedirect().redirect_request(request, None, 302, '', {}, 'https://other.invalid/collect')
            if state.after_cloud_download:
                state.after_cloud_download()
            response = Response(state.cloud_pdf or b'{"actionMsg":{"code":"denied"}}', request.full_url)
            response.headers.update(state.cloud_headers)
            return response
        elif path == '/api/hrm/login/getAccountList':
            body = {'data': {'userid': state.actor}}
        elif path == '/api/doc/detail/basicInfo':
            assert params.get('docid') == ['303']
            if 'requestid' in params:
                assert params['requestid'] == ['101'] and params['isrequest'] == ['1']
                state.initialized = True
            body = {'status': 1, 'mainimagefile': state.main_file,
                    'data': {'docMain': getattr(state, 'doc_main', '')}}
        else:
            assert path == '/weaver/weaver.file.FileDownload'
            assert params == {'fileid': ['202']}
            if not state.initialized or state.still_denied:
                return Response(b'<html>permission denied</html>', 'https://oa.example.test/notice/noright.jsp')
            return Response(state.pdf, request.full_url)
        return Response(json.dumps(body).encode(), request.full_url)

    monkeypatch.setattr(remote.safe_http, 'urlopen', open_request)
    monkeypatch.setattr(original_source.urllib.request, 'build_opener', lambda *_a: SimpleNamespace(open=open_request))
    monkeypatch.setattr(remote.DownloadTransfer, 'browser', staticmethod(lambda *_a, **_kw: pytest.fail('classified denial must stop')))
    return state

def collect_original(state, tmp_path):
    return collect_case('101', oa_api=state.client, configs=[state.client.config],
                        allow_browser_download=True, attachment_root=tmp_path / 'attachments',
                        prepared_root=tmp_path / 'prepared', source_policy=POLICY)

def test_collect_reads_linked_original_body_via_native_context(original_transport, tmp_path):
    state = original_transport
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'content_read'
    assert invoice['files'][0]['resource_id'] == '202'
    assert len(material['resources']) == 1
    assert material['resources'][0]['source_field_path'] == 'detail[0].row[0].field[0]'
    assert material['prepared']['manifest'][0]['page_count'] == 1
    assert NUMBER in material['prepared']['structured_contents'][0]['key_text'][0]
    assert state.initialized
    downloads = [r for r in state.requests if r[0] == '/weaver/weaver.file.FileDownload']
    assert len(downloads) == 2 and downloads[0] == downloads[1]
    assert 'session=fixed' not in json.dumps(material, default=str)


@pytest.fixture(params=['jpg', 'png'])
def image_kind(request):
    return request.param


@pytest.fixture
def jpg_original(original_transport, image_kind):
    state = original_transport
    with fitz.open(stream=state.pdf, filetype='pdf') as document:
        state.pdf = document[0].get_pixmap().tobytes('jpeg' if image_kind == 'jpg' else 'png')
    state.content_type = 'image/jpeg' if image_kind == 'jpg' else 'image/png'
    state.detail['flist'][0]['type'] = image_kind
    state.detail['modify_info']['pdf'] = ''
    return state


@pytest.mark.parametrize('cloud', [False, True])
def test_collect_prepares_linked_jpg_original(jpg_original, tmp_path, cloud):
    state = jpg_original
    state.still_denied = cloud
    state.cloud_pdf = state.pdf if cloud else None
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'content_read'
    assert invoice['files'][0]['resource_id'] == '202'
    assert material['resources'][0]['local_readonly_path'].endswith('.' + state.detail['flist'][0]['type'])
    assert material['prepared']['manifest'][0]['status'] == 'image'
    assert len(material['prepared']['images']) == 1


@pytest.mark.parametrize('truncated', [False, True])
def test_collect_preserves_and_prepares_every_mpo_frame(jpg_original, tmp_path, truncated):
    from PIL import Image

    state = jpg_original
    state.detail['flist'][0]['type'] = 'jpg'
    state.content_type = 'image/jpeg'
    body = BytesIO()
    first = Image.new('RGB', (48, 32), 'red')
    second = Image.new('L', (24, 16), 180)
    first.save(body, format='MPO', save_all=True, append_images=[second])
    original = body.getvalue()
    state.pdf = original[:-100] if truncated else original
    if truncated:
        with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
            collect_original(state, tmp_path)
        return
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'content_read'
    assert invoice['files'][0]['page_count'] == 2
    assert material['prepared']['manifest'][0]['page_count'] == 2
    images = material['prepared']['images']
    assert len(images) == 2
    assert {image['resource_id'] for image in images} == {'202'}
    from pathlib import Path

    assert Path(material['resources'][0]['local_readonly_path']).read_bytes() == original
    for image, size in zip(images, [(48, 32), (24, 16)]):
        with Image.open(image['path']) as prepared:
            assert prepared.format == 'PNG'
            assert prepared.size == size
            prepared.load()


@pytest.mark.parametrize('cloud', [False, True])
@pytest.mark.parametrize('body', ['truncated', 'pdf'])
def test_unreadable_jpg_original_stops_collection(jpg_original, tmp_path, cloud, body):
    state = jpg_original
    if body == 'truncated':
        state.pdf = state.pdf[:100]
    else:
        with fitz.open() as document:
            document.new_page()
            state.pdf = document.tobytes()
    state.still_denied = cloud
    state.cloud_pdf = state.pdf if cloud else None
    with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
        collect_original(state, tmp_path)


@pytest.mark.parametrize('change', ['furl', 'pdf', 'multiple'])
def test_jpg_original_requires_unambiguous_matching_reference(jpg_original, tmp_path, change):
    state = jpg_original
    if change == 'multiple':
        state.detail['flist'].append({'id': '8002', 'type': 'jpg', 'del': 0})
    else:
        state.detail['modify_info'][change] = '8002'
    material = collect_original(state, tmp_path)
    assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'not_read'
    assert not material['resources']
    assert not any(r[0] in {'/weaver/weaver.file.FileDownload', '/invoiceApi/file/download'}
                   for r in state.requests)


@pytest.mark.parametrize('body', ['valid', 'truncated', 'pdf'])
def test_browser_fallback_validates_jpg_original(jpg_original, tmp_path, monkeypatch, body):
    state = jpg_original
    if body == 'truncated':
        state.pdf = state.pdf[:100]
    elif body == 'pdf':
        with fitz.open() as document:
            document.new_page()
            state.pdf = document.tobytes()

    def browser(_resource, *, dest_dir, **_kwargs):
        path = dest_dir / ('browser-original.' + state.detail['flist'][0]['type'])
        path.write_bytes(state.pdf)
        return original_source.Downloaded(path)

    monkeypatch.setattr(remote.DownloadTransfer, 'native', lambda *_a, **_kw: original_source.Unavailable())
    monkeypatch.setattr(remote.DownloadTransfer, 'browser', staticmethod(browser))
    if body == 'valid':
        material = collect_original(state, tmp_path)
        assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'content_read'
    else:
        with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
            collect_original(state, tmp_path)

def test_collect_reads_cloud_original_when_native_document_remains_denied(original_transport, tmp_path):
    state = original_transport
    state.still_denied = True
    state.cloud_pdf = state.pdf
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'content_read'
    assert invoice['files'][0]['original_source']['cloud_file_id'] == '8001'
    assert material['prepared']['manifest'][0]['page_count'] == 1
    assert NUMBER in material['prepared']['structured_contents'][0]['key_text'][0]
    assert sum(r[0] == '/invoiceApi/file/download' for r in state.requests) == 1
    assert 'synthetic-token' not in json.dumps(material, default=str)
    assert not any('synthetic-token' in p.read_text(errors='ignore')
                   for p in (tmp_path / 'attachments').rglob('*.json'))

@pytest.mark.parametrize('kind', ['valid_prefix', 'late_header', 'invalid_body'])
def test_cloud_pdf_preamble_is_parsed_without_changing_original(original_transport, tmp_path, kind):
    state = original_transport
    state.still_denied = True
    prefix = b'\x00\x01synthetic-preamble\x00\x01'
    state.cloud_pdf = (prefix + state.pdf if kind == 'valid_prefix' else
                       b'x' * 1024 + state.pdf if kind == 'late_header' else
                       prefix + b'%PDF-1.7 invalid document %%EOF')
    if kind != 'valid_prefix':
        with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
            collect_original(state, tmp_path)
        assert not list((tmp_path / 'prepared').rglob('*.png'))
        return
    material = collect_original(state, tmp_path)
    resource, = material['resources']
    assert Path(resource['local_readonly_path']).read_bytes() == state.cloud_pdf
    assert resource['resource_id'] == '202'
    assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'content_read'
    assert material['prepared']['manifest'][0]['page_count'] == 1
    assert NUMBER in material['prepared']['structured_contents'][0]['key_text'][0]

def test_cloud_body_is_not_prepared_after_linked_file_changes(original_transport, tmp_path):
    state = original_transport
    state.still_denied = True
    state.cloud_pdf = state.pdf
    state.after_cloud_download = lambda: state.detail['modify_info'].update(furl='8002')
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert not list((tmp_path / 'attachments').rglob('*.cloud.part'))
    assert not list((tmp_path / 'prepared').rglob('*.png'))

@pytest.mark.parametrize('change,expected', [
    ('main_file', 'resource_ownership_untrusted'),
    ('actor', 'rpa_session_invalid'),
    ('data_id', 'resource_ownership_untrusted'),
])
@pytest.mark.parametrize('preamble', [b'', b'\x00synthetic-prefix\x01'])
def test_cloud_download_rechecks_live_identity_before_preparation(original_transport, tmp_path, change, expected, preamble):
    state = original_transport
    state.still_denied = True
    state.cloud_pdf = preamble + state.pdf
    if change == 'data_id':
        field = state.workflow['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields'][0]
        state.after_cloud_download = lambda: field.update(fieldValue='102')
    else:
        state.after_cloud_download = lambda: setattr(state, change, '999')
    with pytest.raises(RuntimeError, match='^' + expected + '$'):
        collect_original(state, tmp_path)
    assert not list((tmp_path / 'attachments').rglob('*.cloud.part'))
    assert not list((tmp_path / 'prepared').rglob('*.png'))

@pytest.mark.parametrize('failure', ['json', 'truncated', 'oversized', 'http', 'redirect'])
def test_cloud_failure_keeps_collection_unavailable(original_transport, tmp_path, monkeypatch, failure):
    state = original_transport
    state.still_denied = True
    state.cloud_pdf = b'{}' if failure == 'json' else state.pdf
    if failure == 'truncated':
        state.cloud_headers['Content-Length'] = str(len(state.pdf) + 1)
    if failure == 'oversized':
        monkeypatch.setattr(original_source, '_MAX_ORIGINAL_BYTES', 16)
    state.cloud_failure = failure
    expected = {'http': 'attachment_access_denied', 'redirect': 'attachment_download_failed'}.get(
        failure, 'attachment_content_incomplete')
    with pytest.raises(RuntimeError, match='^' + expected + '$'):
        collect_original(state, tmp_path)
    assert sum(r[0] == '/invoiceApi/file/download' for r in state.requests) == 1
    assert not list((tmp_path / 'attachments').rglob('*.cloud.part'))

@pytest.mark.parametrize('change', ['request', 'record', 'number', 'cloud_file', 'doc', 'main_file', 'actor'])
def test_untrusted_original_never_reaches_body_preparation(original_transport, tmp_path, change):
    state = original_transport
    if change == 'request':
        state.workflow['requestId'] = '999'
    elif change == 'record':
        state.detail['id'] = '999'
    elif change == 'number':
        state.detail['number'] = '999'
    elif change == 'cloud_file':
        state.detail['modify_info']['furl'] = '999'
    elif change == 'doc':
        state.detail['oa']['doc_id'] = '999'
    elif change == 'main_file':
        state.main_file = '999'
    else:
        state.actor = '88'
    if change == 'cloud_file':
        material = collect_original(state, tmp_path)
        assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'not_read'
        assert not material['resources']
    else:
        with pytest.raises(RuntimeError, match='resource_ownership_untrusted|invoice_buyer_read_failed|rpa_session_invalid'):
            collect_original(state, tmp_path)
    assert not state.initialized
    assert not any(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)

@pytest.mark.parametrize('failure', ['denied', 'invalid_pdf'])
def test_bound_original_download_or_preparation_failure_stops_collection(original_transport, tmp_path, failure):
    state = original_transport
    if failure == 'denied':
        state.still_denied = True
    else:
        state.pdf = b'broken PDF'
    with pytest.raises(RuntimeError):
        collect_original(state, tmp_path)
    assert state.initialized
    assert sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests) == 2

def test_forged_original_hint_cannot_initialize_unlinked_document(original_transport, tmp_path):
    state = original_transport
    state.detail.pop('oa')
    download_result = attachments.download_resources([
        {'id': '202', 'name': 'original.pdf',
         'downloadUrl': 'https://oa.example.test/weaver/weaver.file.FileDownload?fileid=202',
         '_invoice_original': {'document_id': '303', 'native_file_id': '202'}},
    ], dest_dir=tmp_path / 'files', timeout_sec=1, allow_browser_download=True,
        request_id='101', flow_key=CONFIG['flow_key'], sources=attachments.build_download_sources(POLICY))
    files = list(download_result.items)
    errors = [error.to_record() for error in download_result.errors]
    assert not files and errors[0]['code'] == 'resource_ownership_untrusted'
    assert not state.initialized
    assert not any(r[0] == '/api/doc/detail/basicInfo' for r in state.requests)

@pytest.mark.parametrize('other_type', ['ofd', 'jpg'])
def test_multiple_file_records_keep_partial_body_coverage(original_transport, tmp_path, other_type):
    state = original_transport
    state.detail['flist'].append({'id': '8002', 'type': other_type, 'del': 0})
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'partial_read'
    assert [f['body_read_status'] for f in invoice['files']] == ['content_read', 'not_read']
    from apps.oa.approvals.collection.invoice_originals import body_coverage
    assert body_coverage(invoice, material['resources'], material['prepared']) == 'partial_read'


def test_invoice_body_rejects_parent_only_prepared_pages():
    from apps.oa.materials.prepared import read_prepared
    from apps.oa.approvals.collection.invoice_originals import bind_prepared_bodies

    evidence = {'invoices': [{'files': [{'original_source': {'native_file_id': 'receipt'}}]}]}
    prepared = read_prepared({
        'manifest': [{'resource_id': 'receipt', 'status': 'pdf_rendered', 'page_count': 1, 'image_count': 1}],
        'images': [{'resource_id': 'child', 'parent_resource_id': 'receipt', 'source_content_sha256': 'a' * 64}],
    })
    with pytest.raises(RuntimeError, match='^attachment_content_incomplete$'):
        bind_prepared_bodies(evidence, [{'resource_id': 'receipt', 'media_type': 'application/pdf'}], prepared)

@pytest.mark.parametrize('mapping', ['missing_document', 'multiple_pdfs', 'deleted_pdf'])
def test_unmapped_original_keeps_authoritative_invoice_without_guessing_download(original_transport, tmp_path, mapping):
    state = original_transport
    if mapping == 'missing_document':
        state.detail.pop('oa')
    elif mapping == 'multiple_pdfs':
        state.detail['flist'].append({'id': '8002', 'type': 'pdf', 'del': 0})
        state.detail['modify_info']['pdf'] = '8999'
    else:
        state.detail['flist'][0]['del'] = 1
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['invoice_no'] == NUMBER
    assert invoice['body_read_status'] == 'not_read'
    assert not material['resources'] and not material['prepared']['images']
    assert not state.initialized
    assert not any(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)

def test_explicit_original_pdf_is_read_without_selecting_the_other_pdf(original_transport, tmp_path):
    state = original_transport
    state.detail['flist'].append({'id': '8002', 'type': 'pdf', 'del': 0})
    state.detail['modify_info']['fileType'] = '2'
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    selected, other = invoice['files']
    assert invoice['body_read_status'] == 'partial_read'
    assert selected['original_source']['cloud_file_id'] == '8001'
    assert selected['body_read_status'] == 'content_read'
    assert other['file_id'] == '8002' and other['body_read_status'] == 'not_read'
    assert 'original_source' not in other
    assert material['prepared']['manifest'][0]['page_count'] == 1

@pytest.mark.parametrize('change', ['conflict', 'missing', 'deleted', 'duplicate'])
def test_multiple_pdf_selection_rejects_inconsistent_official_mapping(original_transport, change):
    state = original_transport
    state.detail['flist'].append({'id': '8002', 'type': 'pdf', 'del': 0})
    state.detail['modify_info']['fileType'] = '2'
    if change == 'conflict':
        state.detail['modify_info']['furl'] = '8002'
    elif change == 'missing':
        state.detail['modify_info']['pdf'] = '8999'
    elif change == 'deleted':
        state.detail['flist'][0]['del'] = 1
    else:
        state.detail['flist'].append(deepcopy(state.detail['flist'][0]))
    assert invoice_buyers.original_source(state.detail, NUMBER) == {}

def test_original_prepares_every_page_before_claiming_complete_body(original_transport, tmp_path):
    state = original_transport
    with fitz.open(stream=state.pdf, filetype='pdf') as document:
        document.new_page().insert_text((40, 40), 'Invoice continuation page')
        state.pdf = document.tobytes()
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    assert invoice['body_read_status'] == 'content_read'
    assert invoice['files'][0]['page_count'] == 2
    assert material['prepared']['manifest'][0]['page_count'] == 2
    assert len(material['prepared']['images']) == 2
    assert 'Invoice continuation page' in material['prepared']['structured_contents'][0]['key_text'][1]

def test_existing_exact_native_resource_is_downloaded_once(original_transport, tmp_path):
    state = original_transport
    resource = {'id': '202', 'name': 'ordinary.pdf',
                'downloadUrl': 'https://oa.example.test/weaver/weaver.file.FileDownload?fileid=202'}
    state.client.request_context = lambda _request_id: (state.workflow, [resource])
    material = collect_original(state, tmp_path)
    assert len(material['resources']) == 1
    assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'content_read'
    assert sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests) == 2

@pytest.mark.parametrize('workflow_key', [
    'travel_expense', 'business_entertainment', 'business_support_fee',
    'small_marketing_fee', 'tpm_small_marketing_fee', 'general_expense',
])
def test_original_body_uses_shared_finance_collection(workflow_key, original_transport, tmp_path, monkeypatch):
    from apps.oa.approvals.collection import case as case_bundle
    state = original_transport
    config = {**CONFIG, 'flow_key': workflow_key}
    state.client.config = config
    monkeypatch.setattr(flow, 'load_all_configs', lambda: [config])
    monkeypatch.setattr(official_sql, 'travel_extra_fields', lambda _workflow: [])
    monkeypatch.setattr(case_bundle, '_travel_resources', lambda **_kwargs: TravelEvidence.empty())
    material = collect_original(state, tmp_path)
    assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'content_read'
    assert material['prepared']['images']

def test_invoice_document_warning_clears_only_for_exact_read_document(original_transport, tmp_path):
    state = original_transport
    fields = state.workflow['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]['workflowRequestTableFields']
    for docid in ('303', '404'):
        fields.append({'fieldName': 'fpfj', 'fieldShowName': '发票附件',
                       'fieldValue': docid, 'filedHtmlShow': f'<a onclick="toDocument({docid})">原件</a>'})
    material = collect_original(state, tmp_path)
    assert [w['source_field_path'] for w in material['warnings']] == ['detail[0].row[0].field[2]']

@pytest.mark.parametrize('change', ['binding', 'cached_document'])
def test_original_binding_is_rechecked_even_when_download_is_already_readable(original_transport, tmp_path, change):
    state = original_transport
    state.initialized = True
    if change == 'binding':
        original_context = state.client.request_context
        calls = []

        def context(request_id):
            calls.append(request_id)
            if len(calls) > 1:
                state.detail['oa']['doc_id'] = '404'
            return original_context(request_id)

        state.client.request_context = context
    else:
        collect_original(state, tmp_path)
        state.main_file = '999'
    before = sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests) == before

def test_exact_native_deduplication_retains_original_verification(original_transport, tmp_path):
    state = original_transport
    state.initialized = True
    state.main_file = '999'
    state.client.request_context = lambda _request_id: (state.workflow, [
        {'id': '202', 'name': 'ordinary.pdf',
         'downloadUrl': 'https://oa.example.test/weaver/weaver.file.FileDownload?fileid=202'},
    ])
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert not any(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)

@pytest.fixture
def many_originals(original_transport, monkeypatch):
    state = original_transport
    state.details = {}
    state.detail_reads = []
    state.cloud_reads = []
    state.after_body = None
    rows = []
    for index in range(27):
        data_id = str(1000 + index)
        number = str(1000 + index).zfill(20)
        detail = deepcopy(state.detail)
        detail.update(id=str(9000 + index), fid=str(9000 + index), number=number)
        detail['flist'][0]['id'] = str(8000 + index)
        detail['modify_info'] = {'pdf': str(8000 + index), 'furl': str(8000 + index)}
        detail['oa'] = {'doc_id': str(3000 + index), 'img_id': str(2000 + index)}
        state.details[data_id] = detail
        rows.append({'workflowRequestTableFields': [{
            'fieldName': 'glfp', 'fieldShowName': '关联发票',
            'fieldValue': data_id, 'fieldShowValue': number,
        }]})
    state.workflow['workflowDetailTableInfos'][0]['workflowRequestTableRecords'] = rows
    original_get = state.client.get
    state.client.get = lambda path, params=None, *, retry=True: (
        {'status': '1', 'targetIds': state.details[params['ids']]['id']}
        if path == invoice_buyers.MAPPING_PATH else original_get(path, params)
    )
    original_open = remote.safe_http.urlopen

    class Response(BytesIO):
        def __init__(self, body, url):
            super().__init__(body)
            self.url = url
            self.headers = {'Content-Length': str(len(body))}

        def geturl(self):
            return self.url

    def open_request(request, **kwargs):
        path = urlsplit(request.full_url).path
        params = parse_qs(urlsplit(request.full_url).query)
        if request.full_url == invoice_buyers.DETAIL_URL:
            data_id = json.loads(request.data)['dataId']
            state.detail_reads.append(data_id)
            body = json.dumps({'actionMsg': {'code': '0'}, 'data': state.details[data_id]}).encode()
        elif path == '/invoiceApi/file/download':
            state.cloud_reads.append(params['dataId'][0])
            if state.after_body:
                state.after_body()
            body = state.pdf
        elif path == '/api/doc/detail/basicInfo':
            index = int(params['docid'][0]) - 3000
            body = json.dumps({'status': 1, 'mainimagefile': str(2000 + index)}).encode()
        elif path == '/weaver/weaver.file.FileDownload':
            return Response(b'<html>permission denied</html>', 'https://oa.example.test/notice/noright.jsp')
        else:
            return original_open(request, **kwargs)
        return Response(body, request.full_url)

    monkeypatch.setattr(remote.safe_http, 'urlopen', open_request)
    monkeypatch.setattr(original_source.urllib.request, 'build_opener', lambda *_a: SimpleNamespace(open=open_request))
    return state

def test_many_originals_keep_full_body_coverage_with_bounded_context_reads(many_originals, tmp_path):
    material = collect_original(many_originals, tmp_path)
    assert len(material['resources']) == len(material['prepared']['images']) == 27
    assert all(entry['body_read_status'] == 'content_read'
               for entry in material['invoice_evidence']['invoices'])
    assert len(many_originals.cloud_reads) == 27
    assert len(many_originals.detail_reads) <= 27 * 3

def test_many_originals_reject_binding_change_before_preparing_any_body(many_originals, tmp_path):
    many_originals.after_body = lambda: many_originals.details['1000']['oa'].update(doc_id='999')
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(many_originals, tmp_path)
    assert not list((tmp_path / 'prepared').rglob('*.png'))
    many_originals.after_body = None
    many_originals.details['1000']['oa']['doc_id'] = '3000'
    material = collect_original(many_originals, tmp_path)
    assert len(material['prepared']['images']) == 27
    assert len(many_originals.cloud_reads) == 54

def test_original_batch_stop_keeps_downloaded_files_unprepared(many_originals, monkeypatch, tmp_path):
    stopped = []
    monkeypatch.setattr(attachments.runtime_control, 'stop_requested', lambda: bool(stopped))
    many_originals.after_body = lambda: stopped.append(True)
    with pytest.raises(RuntimeError, match='^oa_run_stopped$'):
        collect_original(many_originals, tmp_path)
    assert not list((tmp_path / 'prepared').rglob('*.png'))

@pytest.mark.parametrize('change', ['binding', 'body'])
def test_original_cache_reuse_requires_current_binding_and_bytes(original_transport, tmp_path, change):
    state = original_transport
    state.still_denied = True
    state.cloud_pdf = state.pdf
    material = collect_original(state, tmp_path)
    if change == 'binding':
        state.detail['flist'][0]['id'] = '8002'
        state.detail['modify_info'] = {'pdf': '8002', 'furl': '8002'}
        with fitz.open(stream=state.pdf, filetype='pdf') as document:
            document.new_page().insert_text((40, 40), 'Replacement original')
            state.cloud_pdf = document.tobytes()
    else:
        from pathlib import Path
        Path(material['resources'][0]['local_readonly_path']).write_bytes(b'%PDF changed')
    material = collect_original(state, tmp_path)
    assert sum(r[0] == '/invoiceApi/file/download' for r in state.requests) == 2
    assert len(material['prepared']['images']) == (2 if change == 'binding' else 1)
    assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'content_read'

@pytest.mark.parametrize('failure_point', [2, 3])
def test_original_context_transport_failure_preserves_download_error_contract(
    original_transport, tmp_path, failure_point,
):
    state = original_transport
    context = state.client.request_context
    calls = []

    def failing_context(request_id):
        calls.append(request_id)
        if len(calls) == failure_point:
            raise RuntimeError('synthetic_transport_failed')
        return context(request_id)

    state.client.request_context = failing_context
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert not list((tmp_path / 'prepared').rglob('*.png'))

def test_original_stop_during_final_context_read_prevents_preparation(original_transport, tmp_path, monkeypatch):
    state = original_transport
    context = state.client.request_context
    calls = []
    stopped = []
    monkeypatch.setattr(attachments.runtime_control, 'stop_requested', lambda: bool(stopped))

    def stopping_context(request_id):
        calls.append(request_id)
        if len(calls) == 3:
            stopped.append(True)
        return context(request_id)

    state.client.request_context = stopping_context
    with pytest.raises(RuntimeError, match='^oa_run_stopped$'):
        collect_original(state, tmp_path)
    assert not list((tmp_path / 'prepared').rglob('*.png'))

def test_new_collection_redownloads_unchanged_bound_body(original_transport, tmp_path):
    state = original_transport
    state.still_denied = True
    state.cloud_pdf = state.pdf
    collect_original(state, tmp_path)
    material = collect_original(state, tmp_path)
    assert sum(r[0] == '/invoiceApi/file/download' for r in state.requests) == 2
    assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'content_read'

def test_original_legacy_cache_waits_for_final_binding_check(original_transport, tmp_path, monkeypatch):
    state = original_transport
    collect_original(state, tmp_path)
    entry = attachments.attachment_cache.latest_attachment_file('101', CONFIG['flow_key'], '202')
    writes = []
    monkeypatch.setattr(attachments, '_load_manifest', lambda _root: {'resources': {'202': entry}})
    monkeypatch.setattr(attachments.attachment_cache, 'latest_attachment_file', lambda *_a: None)
    monkeypatch.setattr(attachments.attachment_cache, 'record_attachment_file', lambda **kwargs: writes.append(kwargs))
    context = state.client.request_context
    calls = []

    def changing_context(request_id):
        calls.append(request_id)
        if len(calls) == 3:
            state.detail['oa']['doc_id'] = '404'
        return context(request_id)

    state.client.request_context = changing_context
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert not writes



@pytest.mark.parametrize('cached_state', ['unchanged', 'bytes_changed', 'document_changed'])
def test_explicit_original_cache_reuse_checks_current_document_and_bytes(
    original_transport, tmp_path, cached_state,
):
    from pathlib import Path

    state = original_transport
    material = collect_original(state, tmp_path)
    source = material['invoice_evidence']['invoices'][0]['files'][0]['original_source']
    path = Path(material['resources'][0]['local_readonly_path'])
    if cached_state == 'bytes_changed':
        path.write_bytes(b'%PDF changed')
    elif cached_state == 'document_changed':
        state.main_file = '999'
    targets = [{
        'id': '202', 'name': 'invoice-original-202.pdf',
        'downloadUrl': '/weaver/weaver.file.FileDownload?fileid=202',
        '_invoice_original': source,
    }]
    before = sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)
    download_result = attachments.download_resources(
        targets, dest_dir=path.parent, timeout_sec=1, allow_browser_download=True,
        request_id='101', flow_key=CONFIG['flow_key'], reuse_cache=True,
     sources=attachments.build_download_sources(POLICY))
    files = list(download_result.items)
    errors = [error.to_record() for error in download_result.errors]
    after = sum(r[0] == '/weaver/weaver.file.FileDownload' for r in state.requests)
    if cached_state == 'document_changed':
        assert files == [] and errors[0]['code'] == 'resource_ownership_untrusted'
        assert after == before
    else:
        assert errors == []
        assert files[0]['reused'] is (cached_state == 'unchanged')
        assert after - before == (0 if cached_state == 'unchanged' else 1)
        assert Path(files[0]['path']).read_bytes() == state.pdf


@pytest.mark.parametrize('failure', ['binding_changed', 'stopped'])
def test_failed_mixed_original_batch_never_publishes_cache(
    original_transport, tmp_path, monkeypatch, failure,
):
    state = original_transport
    source = {'cloud_record_id': '9001', 'cloud_file_id': '8001',
              'document_id': '303', 'native_file_id': '202'}
    targets = [
        {'id': '202', 'name': 'invoice-original-202.pdf', '_invoice_original': source,
         'downloadUrl': 'https://oa.example.test/weaver/weaver.file.FileDownload?fileid=202'},
        {'id': 'ordinary', 'name': 'ordinary.pdf', 'allowExtraDownloadHost': True,
         'downloadUrl': 'https://files.example.test/ordinary.pdf'},
    ]
    original_open = remote.safe_http.urlopen

    class Response(BytesIO):
        def __init__(self, url):
            super().__init__(state.pdf)
            self.url = url

        def geturl(self):
            return self.url

    def open_request(request, **kwargs):
        if request.full_url == 'https://files.example.test/ordinary.pdf':
            return Response(request.full_url)
        return original_open(request, **kwargs)

    monkeypatch.setattr(remote.safe_http, 'urlopen', open_request)
    context = state.client.request_context
    reads = []
    stopped = []

    def changing_context(request_id):
        reads.append(request_id)
        if len(reads) == 2:
            if failure == 'binding_changed':
                state.detail['oa']['doc_id'] = '404'
            else:
                stopped.append(True)
        return context(request_id)

    monkeypatch.setattr(attachments.runtime_control, 'stop_requested', lambda: bool(stopped))
    state.client.request_context = changing_context
    destination = tmp_path / 'mixed'
    download_result = attachments.download_resources(
        targets, dest_dir=destination, timeout_sec=1, allow_browser_download=True,
        request_id='101', flow_key=CONFIG['flow_key'],
     sources=attachments.build_download_sources(POLICY))
    files = list(download_result.items)
    errors = [error.to_record() for error in download_result.errors]
    expected = 'resource_ownership_untrusted' if failure == 'binding_changed' else 'oa_run_stopped'
    assert files == [] and errors[0]['code'] == expected
    assert list(destination.glob('*.pdf'))
    state.detail['oa']['doc_id'] = '303'
    stopped.clear()
    state.client.request_context = context
    download_result = attachments.download_resources(
        targets, dest_dir=destination, timeout_sec=1, allow_browser_download=True,
        request_id='101', flow_key=CONFIG['flow_key'], reuse_cache=True,
     sources=attachments.build_download_sources(POLICY))
    files = list(download_result.items)
    errors = [error.to_record() for error in download_result.errors]
    assert errors == []
    assert [item['resource_id'] for item in files] == ['202', 'ordinary']
    assert all(item['reused'] is False for item in files)
    assert files[0]['path'] == files[1]['path']
    assert b'session=fixed' not in b''.join(path.read_bytes() for path in destination.rglob('*') if path.is_file())

@pytest.mark.parametrize('failure,reason', [
    ('json', 'cloud_signature_mismatch'),
    ('truncated', 'cloud_length_mismatch'),
    ('oversized', 'cloud_content_too_large'),
    ('invalid_length', 'cloud_content_length_invalid'),
])
def test_cloud_incomplete_original_preserves_safe_failure_diagnostics(
        original_transport, tmp_path, monkeypatch, failure, reason):
    from apps.oa.approvals.collection.case import CollectionFailure

    state = original_transport
    state.still_denied = True
    state.cloud_pdf = b'{}' if failure == 'json' else state.pdf
    if failure == 'truncated':
        state.cloud_headers['Content-Length'] = str(len(state.pdf) + 1)
    elif failure == 'oversized':
        monkeypatch.setattr(original_source, '_MAX_ORIGINAL_BYTES', 16)
    elif failure == 'invalid_length':
        state.cloud_headers['Content-Length'] = 'invalid'
    with pytest.raises(CollectionFailure, match='^attachment_content_incomplete$') as caught:
        collect_original(state, tmp_path)
    assert caught.value.diagnostics == [{
        'stage': 'invoice_original_download', 'reason': reason, 'file_ref': '202',
    }]
    assert 'synthetic-token' not in json.dumps(caught.value.diagnostics)
    assert not list((tmp_path / 'prepared').rglob('*.png'))


def test_ofd_invoice_reads_its_explicit_official_pdf(original_transport, tmp_path, monkeypatch):
    state = original_transport
    state.detail['flist'].insert(0, {'id': '8000', 'type': 'ofd', 'del': 0})
    state.detail['modify_info'].update(furl='8000', pdf='8001', fileType='4')
    state.cloud_pdf = state.pdf
    monkeypatch.setattr(remote.DownloadTransfer, 'native',
                        lambda *a, **kw: pytest.fail('native OFD must not be treated as PDF'))
    material = collect_original(state, tmp_path)
    invoice = material['invoice_evidence']['invoices'][0]
    pdf, = [file for file in invoice['files'] if file['media_type'] == 'pdf']
    ofd, = [file for file in invoice['files'] if file['media_type'] == 'ofd']
    assert pdf['body_read_status'] == 'content_read'
    assert ofd['body_read_status'] == 'not_read'
    assert pdf['original_source']['original_cloud_file_id'] == '8000'
    assert material['prepared']['manifest'][0]['page_count'] == 1
    assert sum(row[0] == '/invoiceApi/file/download' for row in state.requests) == 1


@pytest.mark.parametrize('change', ['missing_ofd', 'wrong_pdf', 'multiple_ofd'])
def test_ofd_pdf_mapping_requires_a_unique_explicit_pair(original_transport, change):
    state = original_transport
    state.detail['flist'].insert(0, {'id': '8000', 'type': 'ofd', 'del': 0})
    state.detail['modify_info'].update(furl='8000', pdf='8001', fileType='4')
    if change == 'missing_ofd':
        state.detail['modify_info']['furl'] = '7999'
    elif change == 'wrong_pdf':
        state.detail['modify_info']['pdf'] = '8999'
    else:
        state.detail['flist'].append({'id': '8002', 'type': 'ofd', 'del': 0})
    assert invoice_buyers.original_source(state.detail, NUMBER) == {}


@pytest.mark.parametrize('change,reason', [('pdf', 'resource_ownership_untrusted'),
                                            ('ofd', 'resource_ownership_untrusted'),
                                            ('actor', 'rpa_session_invalid')])
def test_official_pdf_rechecks_ofd_pair_and_actor_after_download(original_transport, tmp_path, change, reason):
    state = original_transport
    state.detail['flist'].insert(0, {'id': '8000', 'type': 'ofd', 'del': 0})
    state.detail['modify_info'].update(furl='8000', pdf='8001', fileType='4')
    state.cloud_pdf = state.pdf
    if change == 'actor':
        state.after_cloud_download = lambda: setattr(state, 'actor', '78')
    else:
        state.after_cloud_download = lambda: state.detail['modify_info'].update(
            **{'pdf' if change == 'pdf' else 'furl': '8999'})
    with pytest.raises(RuntimeError, match=reason):
        collect_original(state, tmp_path)
    assert not list((tmp_path / 'prepared').rglob('*.png'))


@pytest.fixture
def ofd_document_attachment(original_transport, monkeypatch):
    from apps.oa.approvals.sources import sql_templates

    state = original_transport
    state.detail['flist'].insert(0, {'id': '8000', 'type': 'ofd', 'del': 0})
    state.detail['modify_info'].update(furl='8000', pdf='8001', fileType='4')
    state.main_file = ''
    state.doc_main = ''
    state.cloud_pdf = state.pdf
    state.document_rows = [{'docid': 303, 'imagefileid': 202, 'imagefilename': 'original.ofd',
                            'doctype': 1, 'isextfile': '1', 'docfiletype': '2'}]
    state.sql_calls = []

    def query(name, params, *, require_connection=False):
        assert name == 'document_ofd_attachment'
        assert params == {'document_id': 303} and require_connection
        rendered = sql_templates.render_query(name, params)
        assert rendered.max_rows == 2 and rendered.timeout_sec == 5
        state.sql_calls.append(name)
        return state.document_rows

    monkeypatch.setattr(official_sql, '_query_rows', query)
    return state


def test_official_pdf_can_verify_the_documents_sole_ofd_attachment(ofd_document_attachment, tmp_path):
    state = ofd_document_attachment
    material = collect_original(state, tmp_path)
    pdf, = [file for file in material['invoice_evidence']['invoices'][0]['files']
            if file['media_type'] == 'pdf']
    assert pdf['body_read_status'] == 'content_read'
    assert len(state.sql_calls) >= 3
    assert sum(row[0] == '/invoiceApi/file/download' for row in state.requests) == 1


@pytest.mark.parametrize('change', ['missing', 'multiple', 'document', 'file', 'role',
                                     'doctype', 'filetype', 'extension', 'doc_body', 'main', 'sql'])
def test_ofd_attachment_requires_a_unique_current_binding(ofd_document_attachment, tmp_path, monkeypatch, change):
    state = ofd_document_attachment
    if change == 'missing':
        state.document_rows = []
    elif change == 'multiple':
        state.document_rows *= 2
    elif change == 'doc_body':
        state.doc_main = 'Unexpected document body'
    elif change == 'main':
        state.main_file = '999'
    elif change == 'sql':
        def unavailable(*_args, **_kwargs):
            raise RuntimeError('official_sql_unavailable')
        monkeypatch.setattr(official_sql, '_query_rows', unavailable)
    else:
        field, value = {'document': ('docid', 999), 'file': ('imagefileid', 999),
                        'role': ('isextfile', '0'), 'doctype': ('doctype', 2),
                        'filetype': ('docfiletype', '1'), 'extension': ('imagefilename', 'other.pdf')}[change]
        state.document_rows[0][field] = value
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert not any(row[0] == '/invoiceApi/file/download' for row in state.requests)
    assert not list((tmp_path / 'prepared').rglob('*.png'))


def test_ofd_attachment_binding_is_rechecked_after_cloud_download(ofd_document_attachment, tmp_path):
    state = ofd_document_attachment
    state.after_cloud_download = lambda: state.document_rows[0].update(imagefileid=999)
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert sum(row[0] == '/invoiceApi/file/download' for row in state.requests) == 1
    assert not list((tmp_path / 'prepared').rglob('*.png'))


def test_regular_pdf_still_requires_its_native_main_file(ofd_document_attachment, tmp_path):
    state = ofd_document_attachment
    state.detail['flist'] = [state.detail['flist'][1]]
    state.detail['modify_info'].update(furl='8001', fileType='1')
    with pytest.raises(RuntimeError, match='^resource_ownership_untrusted$'):
        collect_original(state, tmp_path)
    assert state.sql_calls == []
