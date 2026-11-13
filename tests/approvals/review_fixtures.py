import json

from apps.oa.approvals.store import Store


def completed_review(app, task_id):
    task = app.store.get_task(task_id)
    return {'task_id': task_id, 'material_sha256': task['material_sha256'],
            'status': 'complete', 'reason': 'Synthetic fixture review completed',
            'evidence_files': [task['material_file']]}


def completed_review_file(root, task_id):
    store = Store(root / 'approval.sqlite3', read_only=True)
    try:
        task = store.get_task(task_id)
    finally:
        store.connection.close()
    path = root / f'synthetic-review-{task_id}.json'
    path.write_text(json.dumps({'task_id': task_id, 'material_sha256': task['material_sha256'],
        'status': 'complete', 'reason': 'Synthetic fixture review completed',
        'evidence_files': [task['material_file']]}))
    return path
