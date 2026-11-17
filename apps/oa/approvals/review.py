from pathlib import Path
import hashlib
import json


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_assessment(task, value):
    if not isinstance(value, dict):
        raise ValueError('review_assessment_required')
    if type(value.get('task_id')) is not int or value['task_id'] != task['id']:
        raise ValueError('review_task_mismatch')
    if value.get('material_sha256') != task['material_sha256']:
        raise ValueError('review_material_mismatch')
    if (value.get('status') not in {'complete', 'business_pending', 'incomplete', 'technical_failure'}
            or not isinstance(value.get('reason'), str) or not value['reason'].strip()):
        raise ValueError('review_assessment_invalid')
    if value['status'] == 'business_pending' and (
            not isinstance(value.get('rule_basis'), str) or not value['rule_basis'].strip()):
        raise ValueError('review_rule_basis_required')
    files = value.get('evidence_files')
    if not isinstance(files, list) or not files or any(not isinstance(p, str) for p in files):
        raise ValueError('review_evidence_required')
    directory = Path(task['material_file']).parent.resolve()
    evidence = {}
    for name in files:
        path = Path(name)
        if (not path.is_absolute() or not path.resolve().is_relative_to(directory)
                or not path.is_file() or path.is_symlink()):
            raise ValueError('review_evidence_unbound')
        evidence[str(path)] = file_digest(path)
    return {key: value[key] for key in ('task_id', 'material_sha256', 'status', 'reason', 'rule_basis')
            if key in value} | {'evidence_files': list(evidence), 'evidence_sha256': evidence}


def record_failure(store, task, reason, *, status='technical_failure', recognition=None, observations=None, opinion=None, details=None):
    payload = {'task_id': task['id'], 'run_id': task['last_run_id'],
               'material_sha256': task['material_sha256'], 'error': reason,
               'status': status, 'recognition': recognition, 'observations': observations, 'opinion': opinion, 'details': details}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = store.path.parent / 'review-errors' / task['last_run_id'] / f"{task['id']}-{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(raw)
    elif path.read_bytes() != raw:
        raise ValueError('review_failure_evidence_changed')
    store.assess_review(task, {'task_id': task['id'], 'material_sha256': task['material_sha256'],
        'status': status, 'reason': reason, 'evidence_files': [str(path)],
        'evidence_sha256': {str(path): digest}})
    error = ValueError(reason)
    error.diagnostics_file = str(path)
    return error


def validate_frozen(task):
    frozen = json.loads(task['frozen_json'] or '{}')
    if task['material_file']:
        frozen[task['material_file']] = task['material_sha256']
    for name, digest in frozen.items():
        path = Path(name)
        if not path.is_file() or file_digest(path) != digest:
            error = ValueError('frozen_material_changed')
            error.changed_file = name
            raise error


def validate_readiness(task, recognition):
    from apps.oa.materials.prepared import read_prepared, skipped_video_sources
    from apps.oa.materials.reports import parse_report, prepared_parts

    validate_frozen(task)
    material = json.loads(task['material_json'])
    if task['workflow_key'] == 'overseas_invoice':
        return
    prepared = material.get('prepared') or {}
    empty = (material.get('attachment_presence', {}).get('status') == 'confirmed_empty'
             and material.get('resources') == [])
    restricted_empty = (bool(material.get('travel_read_exception')) and material.get('resources') == []
                        and not any(prepared.get(key) for key in ('manifest', 'images', 'structured_contents')))
    read_prepared(prepared, reading='frozen', confirmed_empty=empty or restricted_empty)
    if task['workflow_key'] == 'packaging_layout_approval':
        return
    if recognition is None and not prepared.get('images'):
        return
    if recognition is None:
        raise ValueError('recognition_not_complete')
    report = parse_report(json.dumps(recognition, ensure_ascii=False).encode())
    if report.get('status') not in {'complete', 'skipped'}:
        raise ValueError('recognition_not_complete')
    expected_binding = {'material_file': task['material_file'], 'material_sha256': task['material_sha256'],
                        'request_id': task['request_id']}
    if report.get('material_binding') != expected_binding:
        raise ValueError('recognition_material_mismatch')
    coverage = 'no_attachments' if empty else ('non_video_only' if skipped_video_sources(prepared.get('manifest', []))
                                               else 'all_prepared_content')
    if report.get('coverage') != coverage or report.get('selection') is not None:
        raise ValueError('recognition_coverage_incomplete')
    planned = [metadata for metadata, _source in prepared_parts(prepared)]
    if report.get('read_plan') != [part['part_id'] for part in planned] or len(report['parts']) != len(planned):
        raise ValueError('recognition_coverage_incomplete')
    for expected, actual in zip(planned, report['parts'], strict=True):
        if not isinstance(actual.get('answer'), str) or not actual['answer'].strip():
            raise ValueError('recognition_not_complete')
        if any(actual.get(key) != expected.get(key) for key in (
                'part_id', 'kind', 'resource_id', 'prepared_resource_id', 'evidence_ref')):
            raise ValueError('recognition_source_mismatch')
        if expected['kind'] == 'image' and actual.get('content_sha256') != expected.get('content_sha256'):
            raise ValueError('recognition_source_mismatch')


def validate_delivery_identity(task, agent_id, material_file, material_sha256):
    if task['status'] != 'reviewing':
        raise ValueError('task_not_reviewable')
    if agent_id != task['agent_id'] or not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError('review_agent_mismatch')
    if material_file != task['material_file'] or material_sha256 != task['material_sha256']:
        raise ValueError('review_material_mismatch')
    validate_frozen(task)


def verify_saved_delivery(review):
    delivery = json.loads(review['delivery_json'])
    if not delivery:
        raise ValueError('review_delivery_required')
    for name, digest in delivery['assessment']['evidence_sha256'].items():
        path = Path(name)
        if not path.is_file() or file_digest(path) != digest:
            raise ValueError('review_evidence_changed')
    return delivery
