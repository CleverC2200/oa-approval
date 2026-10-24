from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import socket

from apps.oa.approvals.store import Store


def emit(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)

def socket_path(root):
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:24]
    return Path('/tmp') / f'oa-{identity}.sock'

def failure(error):
    code = str(error)
    return {'status': 'failed', 'error': code if re.fullmatch('[a-z][a-z0-9_]*', code) else type(error).__name__,
            **({'diagnostics_file': error.diagnostics_file} if getattr(error, 'diagnostics_file', None) else {}),
            **({'diagnostics_write_failed': True} if getattr(error, 'diagnostics_write_failed', False) else {})}

def send(root, request, *, timeout=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path(root)))
        client.sendall((json.dumps(request, ensure_ascii=False) + '\n').encode())
        with client.makefile('r', encoding='utf-8') as incoming:
            return json.loads(incoming.readline())


def query(root, operation, *, run_id=None, task_id=None, full=False):
    database = root / 'approval.sqlite3'
    if not database.is_file():
        return {'status': 'not_initialized'}
    store = Store(database, read_only=True)
    try:
        if operation == 'show':
            details = store.details(task_id)
            if not full:
                details['task'].pop('material_json', None)
                details['task'].pop('frozen_json', None)
                details['attachments'] = [{key: item[key] for key in
                    ('id', 'resource_id', 'parent_resource_id', 'name', 'path', 'sha256')} for item in details['attachments']]
                details['contents'] = [{key: item[key] for key in
                    ('id', 'attachment_id', 'locator', 'source', 'status')} for item in details['contents']]
                details['reviews'] = [{key: value for key, value in item.items()
                    if key not in {'recognition_json', 'frozen_json'}} for item in details['reviews']]
                details['submissions'] = [{key: value for key, value in item.items()
                    if key != 'result_json'} for item in details['submissions']]
            return {'status': 'ok', **details}
        run = store.run(run_id) if run_id is not None else store.latest_run()
        if run is None:
            return {'status': 'no_runs', 'owner': {'reachable': False}}
        tasks = store.tasks(run['id'])
        result = {'status': 'ok', 'run': run, 'summary': dict(Counter(task['status'] for task in tasks)),
                  'tasks': tasks if full else [{key: task[key] for key in
                    ('id', 'request_id', 'workflow_key', 'received_at', 'status')} for task in tasks]}
    finally:
        store.connection.close()
    try:
        response = send(root, {'operation': 'status'}, timeout=0.5)
        owner = {'reachable': True, 'run_id': response['run_id']}
    except (OSError, ValueError, KeyError):
        owner = {'reachable': False}
    return {**result, 'owner': owner}
