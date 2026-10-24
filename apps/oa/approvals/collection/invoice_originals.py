from __future__ import annotations

import re
from apps.oa.approvals.sources.invoices import _positive_id

from apps.oa.approvals.sources import native_documents
from apps.oa.approvals.sources import browser_session as rpa_session
from apps.oa.materials.prepared import PreparedMaterial, read_prepared
from apps.oa.approvals.sources.originals import OriginalContext


_FINANCE_FLOWS = {
    'travel_expense', 'business_entertainment', 'business_support_fee',
    'small_marketing_fee', 'tpm_small_marketing_fee', 'general_expense',
}


def resources(request_id: str, workflow: dict, workflow_key: str, evidence: dict, *, base_url: str) -> list[dict]:

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
            'downloadUrl': base_url + '/weaver/weaver.file.FileDownload?fileid=' + file_id,
            '_trusted_source_field_path': paths[0],
            '_trusted_source_field_name': '关联发票原件',
            '_invoice_original': dict(source),
        })
    return result


def read_original_context(request_id: str, *, source_policy) -> OriginalContext:
    from apps.oa.approvals.sources import oa as api
    from apps.oa.approvals import flows as flow
    from apps.oa.approvals.sources import invoices as invoice_buyers

    client = api.build_api({}, source_policy=source_policy)
    workflow, _ = client.request_context(request_id)
    config = flow.select_config_for_workflow(request_id, workflow, flow.load_all_configs())
    flow.ensure_workflow_matches(request_id, workflow, config)
    workflow_key = config['flow_key']
    if workflow_key not in _FINANCE_FLOWS or str(workflow.get('requestId')) != request_id:
        return OriginalContext(client, {})
    result = invoice_buyers.read_invoice_buyers(
        client, workflow, linked_detail_only=workflow_key == 'tpm_small_marketing_fee',
        include_evidence=True,
    )
    evidence = result.get('invoice_evidence') or {}
    trusted = resources(request_id, workflow, workflow_key, evidence,
                        base_url=source_policy.attachments.oa_origin)
    candidates = {item['id']: [] for item in trusted}
    for invoice in evidence.get('invoices', []):
        for file in invoice['files']:
            source = file.get('original_source') or {}
            native_file_id = source.get('native_file_id')
            if native_file_id in candidates:
                candidates[native_file_id].append((source, _positive_id(invoice.get('data_id'))))
    return OriginalContext(client, {
        native_file_id: sources[0]
        for native_file_id, sources in candidates.items()
        if sources and all(source == sources[0][0] and data_id for source, data_id in sources)
    })


def initialize_workflow_document(resource: dict, url: str, *, timeout_sec: int, source_policy) -> bool:
    from apps.oa.approvals.sources import official_sql

    base_url = source_policy.attachments.oa_origin
    if not native_documents.accepts(resource, url, base_url=base_url):
        return False
    request_id = str(resource.get('_workflow_request_id') or '')
    file_id = str(resource.get('id') or '')
    try:
        document_id = official_sql.workflow_attachment_document(request_id, file_id)
        if not document_id:
            context = read_original_context(request_id, source_policy=source_policy)
            document_id = context.sources.get(file_id, ({}, ''))[0].get('document_id', '')
        return native_documents.verify(resource, url, base_url=base_url, document_id=document_id,
                                       timeout_sec=timeout_sec)
    except rpa_session.RpaSessionError:
        raise
    except Exception:
        return False


def prepared_body(source: dict, resource_rows: list, prepared: PreparedMaterial) -> dict:
    resource_id = source['native_file_id']
    resource = [item for item in resource_rows if item.get('resource_id') == resource_id]
    coverage = prepared.coverage(resource_id)
    if (len(resource) != 1 or resource[0].get('media_type') != 'application/pdf'
            or coverage is None or coverage.manifest_count != 1 or coverage.status != 'pdf_rendered'
            or not coverage.direct_image_count or coverage.declared_page_count != coverage.direct_image_count
            or coverage.declared_image_count != coverage.direct_image_count
            or coverage.direct_source_sha256 is None):
        raise RuntimeError('attachment_content_incomplete')
    return {'resource_id': resource_id, 'source_content_sha256': coverage.direct_source_sha256,
            'page_count': coverage.direct_image_count, 'body_read_status': 'content_read'}


def bind_prepared_bodies(evidence: dict, resource_rows: list, prepared: PreparedMaterial) -> None:
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
    prepared_result = read_prepared(prepared)
    files = invoice.get('files') or []
    if not isinstance(files, list) or any(not isinstance(file, dict) for file in files):
        return 'unknown'
    for file in files:
        if file.get('body_read_status') == 'content_read':
            try:
                expected = prepared_body(file['original_source'], resource_rows, prepared_result)
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
