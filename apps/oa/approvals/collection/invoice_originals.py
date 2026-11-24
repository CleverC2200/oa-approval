from __future__ import annotations

from contextlib import contextmanager
import re
import time
from apps.oa.approvals import flows as flow
from apps.oa.approvals.sources.invoices import OFFICIAL_PAGE_PDF, _positive_id, original_resource_id

from apps.oa.approvals.sources import native_documents
from apps.oa.approvals.sources import browser_session as rpa_session
from apps.oa.materials.prepared import PreparedMaterial, PreparationFailure, read_prepared
from apps.oa.approvals.sources.originals import CLOUD_DOWNLOAD_URL, OriginalContext


_FINANCE_FLOWS = {
    'travel_expense', 'business_entertainment', 'business_support_fee',
    'small_marketing_fee', 'tpm_small_marketing_fee', 'general_expense',
}


def _binding_failure(invoice: dict, index: int) -> dict | None:
    if binding := invoice.get('original_binding_diagnostic'):
        return {**binding, 'source': 'invoice_cloud', 'source_path': f'invoice_evidence.invoices[{index}]'}
    return None


def collection_diagnostics(evidence: dict, failures: list[dict]) -> list[dict]:
    diagnostics = []
    by_resource_id: dict[str, list[str]] = {}
    for index, invoice in enumerate(evidence.get('invoices', [])):
        if binding := _binding_failure(invoice, index):
            diagnostics.append(binding)
        for file in invoice.get('files', []):
            if source := file.get('original_source'):
                by_resource_id.setdefault(original_resource_id(source), []).append(f'invoice_evidence.invoices[{index}]')
    for failure in failures:
        file_id = failure.get('file_ref') or failure.get('attachment_ref')
        paths = by_resource_id.get(file_id)
        if not paths:
            diagnostics.append(failure)
            continue
        stage = 'invoice_original_preparation' if failure['stage'] == 'preparation' else failure['stage']
        safe = {key: failure[key] for key in ('reason', 'elapsed_ms', 'http_status', 'exception_type') if key in failure}
        diagnostics.extend({**safe, 'stage': stage, 'source': failure.get('source', 'invoice_original'),
                            'source_path': path} for path in paths)
    return diagnostics


@contextmanager
def preserve_collection_diagnostics(evidence: dict, stage: str):
    try:
        yield
    except Exception as error:
        if evidence.get('invoices'):
            failures = getattr(error, 'diagnostics', None) or [
                {'stage': stage, 'reason': f'{stage}_exception', 'exception_type': type(error).__name__},
            ]
            error.diagnostics = collection_diagnostics(evidence, list(failures))
        raise


def readiness_diagnostics(material: dict) -> list[dict]:
    if material.get('workflow_key') not in _FINANCE_FLOWS:
        return []
    result = []
    for index, invoice in enumerate(material.get('invoice_evidence', {}).get('invoices', [])):
        if (body_coverage(invoice, material.get('resources', []), material.get('prepared', {}))
                not in {'content_read', 'partial_read'}
                or not any(file.get('original_source') and file.get('body_read_status') == 'content_read'
                           for file in invoice.get('files', []))):
            path = f'invoice_evidence.invoices[{index}]'
            result.append({'stage': 'invoice_original_coverage', 'reason': 'required_original_unread',
                           'source_path': path})
            if binding := _binding_failure(invoice, index):
                result.append(binding)
    return result


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
    native_identities = {}
    page_resources = {}
    for invoice, file in originals:
        source = file['original_source']
        file_id = original_resource_id(source)
        if source.get('representation') == OFFICIAL_PAGE_PDF:
            identity = {key: value for key, value in source.items() if key not in {'oa_data_id', 'source_field_paths'}}
            if file_id not in page_resources:
                references: list[dict] = []
                resource = {
                    'id': file_id, 'name': f"invoice-original-{source['cloud_file_id']}.pdf",
                    'downloadUrl': CLOUD_DOWNLOAD_URL, 'source': 'invoice_cloud',
                    'source_request_id': request_id,
                    '_trusted_source_field_name': '关联发票原件',
                    '_trusted_source_references': references,
                    '_invoice_original': dict(identity, request_id=request_id, workflow_key=workflow_key,
                                              oa_references=references, **flow.workflow_identity(workflow)),
                }
                page_resources[file_id] = (identity, resource)
                result.append(resource)
            previous_identity, resource = page_resources[file_id]
            if previous_identity != identity or not source['source_field_paths']:
                raise RuntimeError('resource_ownership_untrusted')
            resource['_trusted_source_references'].extend(_page_references(source))
            continue
        identity = (source['cloud_record_id'], source['cloud_file_id'], source['document_id'])
        paths = invoice.get('source_field_paths') or []
        if native_identities.setdefault(file_id, identity) != identity or not paths:
            raise RuntimeError('resource_ownership_untrusted')
        result.append({
            'id': file_id, 'name': f'invoice-original-{file_id}.{source.get("file_type", "pdf")}',
            'downloadUrl': base_url + '/weaver/weaver.file.FileDownload?fileid=' + file_id,
            '_trusted_source_field_path': paths[0],
            '_trusted_source_field_name': '关联发票原件',
            '_invoice_original': dict(source),
        })
    for _, resource in page_resources.values():
        references = resource['_trusted_source_references']
        owners = {reference['owner_detail_id'] for reference in references}
        paths = {reference['source_field_path'] for reference in references}
        resource['_trusted_owner_detail_id'] = next(iter(owners)) if len(owners) == 1 else None
        resource['_trusted_source_field_path'] = next(iter(paths)) if len(paths) == 1 else ''
    return result


def _page_references(source: dict) -> list[dict]:
    return [
        {'source_field_path': path, 'source_field_name': '关联发票原件',
         'owner_detail_id': path.rpartition('.field[')[0], 'oa_data_id': source['oa_data_id']}
        for path in source['source_field_paths']
    ]


def read_original_context(request_id: str, *, source_policy) -> OriginalContext:
    from apps.oa.approvals.sources import oa as api
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
    sources = {}
    native_sources = {}
    for item in trusted:
        source = item['_invoice_original']
        if source.get('representation') == OFFICIAL_PAGE_PDF:
            sources[item['id']] = (source, source['oa_references'][0]['oa_data_id'])
        else:
            native_sources[item['id']] = source
    candidates = {resource_id: [] for resource_id in native_sources}
    for invoice in evidence.get('invoices', []):
        for file in invoice['files']:
            if source := file.get('original_source'):
                resource_id = original_resource_id(source)
                if resource_id in candidates:
                    candidates[resource_id].append((source, _positive_id(invoice.get('data_id'))))
    sources.update({
        resource_id: (native_sources[resource_id], bindings[0][1])
        for resource_id, bindings in candidates.items()
        if bindings and all(source == bindings[0][0] and data_id for source, data_id in bindings)
    })
    return OriginalContext(client, sources)


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
    resource_id = original_resource_id(source)
    resource = [item for item in resource_rows if item.get('resource_id') == resource_id]
    coverage = prepared.coverage(resource_id)
    file_type = source.get('file_type', 'pdf')
    if file_type not in {'pdf', 'jpg', 'png'} or coverage is None:
        raise RuntimeError('attachment_content_incomplete')
    image = file_type in {'jpg', 'png'}
    media_type = {'jpg': 'image/jpeg', 'png': 'image/png', 'pdf': 'application/pdf'}[file_type]
    statuses = {'image', 'image_converted'} if image else {'pdf_rendered'}
    page_count = (coverage.declared_page_count or 1) if image else coverage.declared_page_count
    if (len(resource) != 1 or resource[0].get('media_type') != media_type
            or coverage.manifest_count != 1 or coverage.status not in statuses
            or not coverage.direct_image_count or page_count != coverage.direct_image_count
            or coverage.declared_image_count != coverage.direct_image_count
            or coverage.direct_source_sha256 is None):
        raise RuntimeError('attachment_content_incomplete')
    if source.get('representation') == OFFICIAL_PAGE_PDF:
        references = resource[0].get('source_references')
        if (resource[0].get('source_system') != 'invoice_cloud' or not isinstance(references, list)
                or any(not isinstance(reference, dict) for reference in references)
                or [reference for reference in references if reference.get('oa_data_id') == source['oa_data_id']]
                != _page_references(source)):
            raise RuntimeError('attachment_content_incomplete')
    return {'resource_id': resource_id, 'source_content_sha256': coverage.direct_source_sha256,
            'page_count': coverage.direct_image_count, 'body_read_status': 'content_read'}


def bind_prepared_bodies(evidence: dict, resource_rows: list, prepared: PreparedMaterial) -> None:
    for index, invoice in enumerate(evidence.get('invoices', [])):
        files = invoice.get('files') or []
        for file in files:
            if source := file.get('original_source'):
                started = time.monotonic()
                try:
                    file.update(prepared_body(source, resource_rows, prepared))
                except RuntimeError as error:
                    raise PreparationFailure([{
                        'stage': 'invoice_original_coverage', 'reason': 'required_original_unread',
                        'source': 'invoice_original', 'source_path': f'invoice_evidence.invoices[{index}]',
                        'elapsed_ms': max(0, round((time.monotonic() - started) * 1000)),
                    }]) from error
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
    read = {file['original_source'].get('document_id') for invoice in evidence.get('invoices', [])
            for file in invoice.get('files', []) if file.get('body_read_status') == 'content_read' and file.get('original_source')}
    return bool(documents) and documents <= read
