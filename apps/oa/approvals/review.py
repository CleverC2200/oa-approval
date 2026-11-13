from pathlib import Path
import hashlib
import json


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
        evidence[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
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
