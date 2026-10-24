from __future__ import annotations

import re
import hashlib
from collections.abc import Callable
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from apps.oa.runtime import files as material_store
from apps.oa.approvals.sources import native_documents
from apps.oa.approvals.sources import browser_session as rpa_session
from apps.oa.runtime import control as runtime_control


_MAX_ORIGINAL_BYTES = 50 * 1024 * 1024
_CLOUD_DOWNLOAD_URL = 'https://api.mypiaojia.com/invoiceApi/file/download'


_FINANCE_FLOWS = {
    'travel_expense', 'business_entertainment', 'business_support_fee',
    'small_marketing_fee', 'tpm_small_marketing_fee', 'general_expense',
}


def _positive_id(value: object) -> str:
    text = str(value) if type(value) in (str, int) else ''
    return text if re.fullmatch(r'[1-9][0-9]*', text) else ''


def original_source(data: dict, invoice_number: str) -> dict:
    record_id = _positive_id(data.get('id'))
    if not record_id or str(data.get('number')) != invoice_number:
        return {}
    if data.get('fid') is not None and str(data['fid']) != record_id:
        return {}
    active = [item for item in data.get('flist') or [] if isinstance(item, dict)
              and item.get('del', 0) in (0, '0') and item.get('type') == 'pdf']
    if len(active) != 1:
        return {}
    cloud_file = _positive_id(active[0].get('id'))
    modified = data.get('modify_info')
    native = data.get('oa')
    if not cloud_file or not isinstance(modified, dict) or not isinstance(native, dict):
        return {}
    doc_id = _positive_id(native.get('doc_id'))
    file_id = _positive_id(native.get('img_id'))
    if (not doc_id or not file_id or str(modified.get('pdf')) != cloud_file
            or str(modified.get('furl')) != cloud_file):
        return {}
    return {'cloud_record_id': record_id, 'cloud_file_id': cloud_file,
            'document_id': doc_id, 'native_file_id': file_id}


def resources(request_id: str, workflow: dict, workflow_key: str, evidence: dict) -> list[dict]:
    from apps.oa.approvals.collection import downloads as attachments

    invoices = evidence.get('invoices') or []
    originals = [(invoice, file) for invoice in invoices for file in invoice.get('files', [])
                 if file.get('original_source')]
    if not originals or workflow_key not in _FINANCE_FLOWS:
        return []
    if (not _positive_id(request_id) or str(workflow.get('requestId')) != request_id
            or evidence.get('read_status') != 'content_read'):
        raise RuntimeError('resource_ownership_untrusted')
    result = []
    identities = {}
    for invoice, file in originals:
        source = file['original_source']
        file_id = source['native_file_id']
        identity = (source['cloud_record_id'], source['cloud_file_id'], source['document_id'])
        if file_id in identities and identities[file_id] != identity:
            raise RuntimeError('resource_ownership_untrusted')
        identities[file_id] = identity
        paths = invoice.get('source_field_paths') or []
        if not paths:
            raise RuntimeError('resource_ownership_untrusted')
        result.append({
            'id': file_id, 'name': f'invoice-original-{file_id}.pdf',
            'downloadUrl': attachments.OA_HOST + '/weaver/weaver.file.FileDownload?fileid=' + file_id,
            '_trusted_source_field_path': paths[0],
            '_trusted_source_field_name': '关联发票原件',
            '_invoice_original': dict(source),
        })
    return result


def _download_contexts(request_id: str):
    from apps.oa.approvals.sources import oa as api
    from apps.oa.approvals import flows as flow
    from apps.oa.approvals.sources import invoices as invoice_buyers

    client = api.build_api({})
    workflow, _ = client.request_context(request_id)
    config = flow.select_config_for_workflow(request_id, workflow, flow.load_all_configs())
    flow.ensure_workflow_matches(request_id, workflow, config)
    workflow_key = config['flow_key']
    if workflow_key not in _FINANCE_FLOWS or str(workflow.get('requestId')) != request_id:
        return client, {}
    result = invoice_buyers.read_invoice_buyers(
        client, workflow, linked_detail_only=workflow_key == 'tpm_small_marketing_fee',
        include_evidence=True,
    )
    evidence = result.get('invoice_evidence') or {}
    trusted = resources(request_id, workflow, workflow_key, evidence)
    candidates = {item['id']: [] for item in trusted}
    for invoice in evidence.get('invoices', []):
        for file in invoice['files']:
            source = file.get('original_source') or {}
            native_file_id = source.get('native_file_id')
            if native_file_id in candidates:
                candidates[native_file_id].append((source, _positive_id(invoice.get('data_id'))))
    return client, {
        native_file_id: sources[0]
        for native_file_id, sources in candidates.items()
        if sources and all(source == sources[0][0] and data_id for source, data_id in sources)
    }


class DownloadBatch:
    def __init__(self, request_id: str, *, base_url: str, cookie_header: Callable,
                 native_read: Callable):
        self.request_id = request_id
        self.base_url = base_url
        self._cookie_header = cookie_header
        self._native_read = native_read
        self.client, self._sources = _download_contexts(request_id)

    def context(self, request_id: str, native_file_id: str):
        source, data_id = ({}, '')
        if request_id == self.request_id:
            source, data_id = self._sources.get(native_file_id, ({}, ''))
        return self.client, source, data_id

    def _checked_resource(self, resource: dict) -> dict:
        from apps.oa.approvals.collection.downloads import normalize_download_url

        url = normalize_download_url(
            str(resource.get('downloadUrl') or resource.get('download_url') or ''),
            oa_host=self.base_url,
        )
        return dict(resource, downloadUrl=url, _ephemeral_cookie_header=self._cookie_header(
            urllib.parse.urlsplit(self.base_url).hostname or ''))

    def _verify(self, resource: dict, *, timeout_sec: int, initialize: bool = False) -> bool:
        source, _ = self._sources.get(str(resource.get('id') or ''), ({}, ''))
        if not source or source != resource.get('_invoice_original'):
            return False
        return native_documents.verify(
            resource, str(resource.get('downloadUrl') or resource.get('download_url') or ''),
            base_url=self.base_url, document_id=source['document_id'],
            timeout_sec=timeout_sec, initialize=initialize,
        )

    def prepare(self, resources: list[dict], *, timeout_sec: int) -> list[dict]:
        for resource in resources:
            if not resource.get('_invoice_original'):
                continue
            try:
                valid = self._verify(self._checked_resource(resource), timeout_sec=timeout_sec)
            except rpa_session.RpaSessionError as error:
                return [{'code': error.code, 'message': '发票原件会话核验失败'}]
            except ValueError:
                valid = False
            if not valid:
                return [{'code': 'resource_ownership_untrusted', 'message': '发票原件归属核验失败'}]
        return []

    def accept_cache(self, resource: dict, entry: dict, path: Path) -> dict | None:
        if not resource.get('_invoice_original'):
            return entry
        if (entry.get('invoice_original_source') == resource['_invoice_original']
                and entry.get('content_sha256')
                and entry['content_sha256'] == _content_sha256(path)):
            return entry
        return None

    def cache_metadata(self, resource: dict, path: Path) -> dict | None:
        if not resource.get('_invoice_original'):
            return {}
        digest = _content_sha256(path)
        return {'invoice_original_source': dict(resource['_invoice_original']),
                'content_sha256': digest} if digest else None

    def download(self, resource: dict, *, dest_dir: Path, timeout_sec: int) -> tuple[Path | None, str]:
        checked = self._checked_resource(resource)
        url = checked['downloadUrl']
        path, error = self._native_read(checked, url, dest_dir=dest_dir, timeout_sec=timeout_sec)
        if path is not None or not error.startswith('attachment_access_denied'):
            return path, error
        try:
            initialized = self._verify(checked, timeout_sec=timeout_sec, initialize=True)
        except rpa_session.RpaSessionError as failure:
            return None, f'{failure.code}: {failure}'
        if not initialized:
            return path, error
        if runtime_control.stop_requested():
            return None, runtime_control.STOP_CODE
        path, retry_error = self._native_read(checked, url, dest_dir=dest_dir, timeout_sec=timeout_sec)
        if path is None and retry_error.startswith('attachment_access_denied'):
            from apps.oa.approvals.collection.downloads import deterministic_filename

            path, cloud_error = self._download_cloud(
                checked, dest=dest_dir / deterministic_filename(resource), timeout_sec=timeout_sec,
            )
            return path, f'{cloud_error}: 关联发票原件读取失败' if cloud_error else ''
        return path, retry_error or (error if path is None else '')

    def finish(self) -> list[dict]:
        if runtime_control.stop_requested():
            return [runtime_control.stop_error('已请求停止财务预审，停止本单附件下载')]
        try:
            _, current = _download_contexts(self.request_id)
            valid = bool(self._sources) and current == self._sources
        except rpa_session.RpaSessionError as error:
            return [{'code': error.code, 'message': '发票原件会话核验失败'}]
        except Exception:
            valid = False
        if runtime_control.stop_requested():
            return [runtime_control.stop_error('已请求停止财务预审，停止本单附件下载')]
        return [] if valid else [{'code': 'resource_ownership_untrusted', 'message': '发票原件批量下载归属已变化'}]

    def _download_cloud(self, resource: dict, *, dest: Path, timeout_sec: int) -> tuple[Path | None, str]:
        from apps.oa.approvals.sources import invoices as invoice_buyers

        request_id = str(resource.get('_workflow_request_id') or '')
        native_file_id = str(resource.get('id') or '')
        try:
            client, source, data_id = self.context(request_id, native_file_id)
            if not source or source != resource.get('_invoice_original'):
                return None, 'resource_ownership_untrusted'
            token_response = client.get(invoice_buyers.TOKEN_PATH)
            token = token_response.get('token')
            if str(token_response.get('status')) != '1' or not isinstance(token, str) or not token:
                return None, 'attachment_access_denied'
            if runtime_control.stop_requested():
                return None, runtime_control.STOP_CODE
            query = urllib.parse.urlencode({'id': source['cloud_file_id'],
                                           'fid': source['cloud_record_id'],
                                           'dataId': data_id, 'token': token})
            request = urllib.request.Request(
                _CLOUD_DOWNLOAD_URL + '?' + query,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://inc.mypiaojia.com/'},
            )
            with urllib.request.build_opener(_NoCloudRedirect()).open(request, timeout=max(1, timeout_sec)) as response:
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isdecimal() or int(length) > _MAX_ORIGINAL_BYTES):
                    return None, 'attachment_content_incomplete'
                body = response.read(_MAX_ORIGINAL_BYTES + 1)
                if (len(body) > _MAX_ORIGINAL_BYTES or not body.startswith(b'%PDF-')
                        or length is not None and len(body) != int(length)):
                    return None, 'attachment_content_incomplete'
            if not self._verify(resource, timeout_sec=timeout_sec):
                return None, 'resource_ownership_untrusted'
            if runtime_control.stop_requested():
                return None, runtime_control.STOP_CODE
            material_store.write_bytes(dest, body, source={
                'request_id': request_id, 'resource_id': native_file_id, 'kind': 'invoice_original',
            })
            return dest, ''
        except urllib.error.HTTPError as error:
            return None, 'attachment_access_denied' if error.code in (401, 403) else 'attachment_download_failed'
        except rpa_session.RpaSessionError as error:
            return None, error.code
        except ValueError as error:
            return None, str(error) if str(error).startswith('material_') else 'attachment_download_failed'
        except Exception:
            return None, 'attachment_download_failed'


def _content_sha256(path: Path) -> str:
    try:
        with path.open('rb') as source:
            return hashlib.file_digest(source, 'sha256').hexdigest()
    except OSError:
        return ''


def source_for_download(request_id: str, native_file_id: str) -> dict:
    _, sources = _download_contexts(request_id)
    source, _ = sources.get(native_file_id, ({}, ''))
    return source


class _NoCloudRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('attachment_download_failed')


def prepared_body(source: dict, resource_rows: list, prepared: dict) -> dict:
    resource_id = source['native_file_id']
    resource = [item for item in resource_rows if item.get('resource_id') == resource_id]
    manifest = [item for item in prepared.get('manifest', []) if item.get('resource_id') == resource_id]
    images = [item for item in prepared.get('images', []) if item.get('resource_id') == resource_id]
    hashes = {item.get('source_content_sha256') for item in images}
    if (len(resource) != 1 or resource[0].get('media_type') != 'application/pdf'
            or len(manifest) != 1 or manifest[0].get('status') != 'pdf_rendered'
            or not images or manifest[0].get('page_count') != len(images)
            or manifest[0].get('image_count') != len(images) or len(hashes) != 1):
        raise RuntimeError('attachment_content_incomplete')
    digest = hashes.pop()
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise RuntimeError('attachment_content_incomplete')
    return {'resource_id': resource_id, 'source_content_sha256': digest,
            'page_count': len(images), 'body_read_status': 'content_read'}


def bind_prepared_bodies(evidence: dict, resource_rows: list, prepared: dict) -> None:
    for invoice in evidence.get('invoices', []):
        files = invoice.get('files') or []
        for file in files:
            if file.get('original_source'):
                file.update(prepared_body(file['original_source'], resource_rows, prepared))
        read = sum(file.get('body_read_status') == 'content_read' for file in files)
        invoice['body_read_status'] = 'content_read' if files and read == len(files) else 'partial_read' if read else 'not_read'


def body_coverage(invoice: dict, resource_rows: list, prepared: dict) -> str:
    if not isinstance(resource_rows, list) or not isinstance(prepared, dict):
        return 'unknown'
    for rows in (resource_rows, prepared.get('manifest', []), prepared.get('images', [])):
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            return 'unknown'
    files = invoice.get('files') or []
    if not isinstance(files, list) or any(not isinstance(file, dict) for file in files):
        return 'unknown'
    for file in files:
        if file.get('body_read_status') == 'content_read':
            try:
                expected = prepared_body(file['original_source'], resource_rows, prepared)
                if any(file.get(key) != value for key, value in expected.items()):
                    return 'unknown'
            except (KeyError, TypeError, RuntimeError):
                return 'unknown'
        elif file.get('body_read_status') != 'not_read':
            return 'unknown'
    read = sum(file.get('body_read_status') == 'content_read' for file in files)
    expected = 'content_read' if files and read == len(files) else 'partial_read' if read else 'not_read'
    return expected if invoice.get('body_read_status') == expected else 'unknown'


def field_body_read(field: dict, evidence: dict) -> bool:
    documents = set(re.findall(r'\btoDocument\s*\(\s*([1-9][0-9]*)\s*\)', field['attachment_html']))
    read = {file['original_source']['document_id'] for invoice in evidence.get('invoices', [])
            for file in invoice.get('files', []) if file.get('body_read_status') == 'content_read' and file.get('original_source')}
    return bool(documents) and documents <= read
