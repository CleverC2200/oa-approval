from hashlib import sha256
import json
import re
from urllib.parse import parse_qsl, urlsplit

from apps.oa.approvals import forms
from apps.oa.approvals.flows import workflow_identity
from apps.oa.materials.resources import attachment_resources, resource_key, resource_name


def validate(value, request_id, workflow_key):
    if (workflow_key != 'tpm_small_marketing_fee' or not isinstance(value, dict)
            or set(value) != {'request_id', 'resource_id', 'authorization_note'}
            or value['request_id'] != str(request_id)
            or not isinstance(value['resource_id'], str) or not re.fullmatch(r'[1-9][0-9]*', value['resource_id'])
            or not isinstance(value['authorization_note'], str) or not value['authorization_note'].strip()):
        raise ValueError('attachment_read_exception_invalid')
    return value


def select(workflow, raw, resources, resource_id):
    matches = [item for item in resources if resource_key(item) == resource_id]
    if len(matches) != 1:
        raise ValueError('attachment_read_exception_target_changed')
    item = matches[0]
    field = next((field for field in forms.extract_fields(workflow)
                  if field['path'] == item.get('_trusted_source_field_path')), None)
    if (field is None or field['field_name'] in {'fpfj', 'glfp'} or '发票' in field['name']
            or not resource_name(item)):
        raise ValueError('attachment_read_exception_target_changed')
    logical_ids = {resource_key(resource) for resource in resources}
    if any(resource_name(resource) == resource_name(item) and resource_key(resource) not in logical_ids
           for resource in attachment_resources(raw)):
        raise ValueError('attachment_read_exception_ambiguous_source')
    return {
        'resource_id': resource_id,
        'name': resource_name(item),
        'source_field_path': field['path'],
        'source_field_name': field['name'],
        'owner_detail_id': item.get('_trusted_owner_detail_id'),
        'read_status': 'skipped_by_user',
    }


def context_digest(workflow, raw, *, origins):
    resources = []
    for item in raw:
        record = dict(item)
        for key in ('downloadUrl', 'download_url'):
            if not record.get(key):
                continue
            url = urlsplit(origins.download_url(record[key], allow_extra=bool(item.get('allowExtraDownloadHost'))))
            query = parse_qsl(url.query, keep_blank_values=True)
            if url.path == '/weaver/weaver.file.FileDownload' and any(name == 'ddcode' for name, _ in query):
                query = [(name, resource_key(item) if name == 'fileid' and not value.isdecimal() else value)
                         for name, value in query]
            record[key] = (url.scheme, url.netloc, url.path, query, url.fragment)
        resources.append(json.dumps(record, sort_keys=True, ensure_ascii=False, default=str))
    annexes = [log['annexDocHtmls'] for log in workflow.get('workflowRequestLogs') or []
               if isinstance(log, dict) and log.get('annexDocHtmls')]
    evidence = [str(workflow.get('requestId')), workflow_identity(workflow),
                forms.extract_fields(workflow), sorted(resources), annexes]
    return sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def recheck(oa_api, material, *, origins):
    scope = material['attachment_read_exception']
    workflow, raw = oa_api.request_context(material['request_id'])
    if context_digest(workflow, raw, origins=origins) != scope['form_snapshot_sha256']:
        raise ValueError('attachment_read_exception_target_changed')
