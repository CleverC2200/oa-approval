from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path

from apps.oa.approvals import receipts


PATH_FIELDS = {'path', 'source_path', 'local_readonly_path', 'source_image_path',
               'material_file', 'rule_file', 'report_file', 'observations_file'}


def referenced_paths(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PATH_FIELDS and isinstance(item, str) and Path(item).is_absolute():
                found.add(Path(item))
            found.update(referenced_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.update(referenced_paths(item))
    return found


def task_paths(store, task):
    paths = {Path(task[key]) for key in ('material_file', 'rule_file') if task[key]}
    paths.update(Path(path) for path in json.loads(task['frozen_json'] or '{}'))
    paths.update(referenced_paths(json.loads(task['material_json'] or '{}')))
    for review in store.connection.execute('SELECT * FROM reviews WHERE task_id=?', (task['id'],)):
        paths.update(Path(path) for path in json.loads(review['frozen_json']))
        paths.update(referenced_paths(json.loads(review['recognition_json'])))
    return paths


def verified_tasks(store):
    tasks = []
    for row in store.connection.execute("SELECT id,request_id,workflow_key,last_run_id,last_seen_at,error FROM tasks WHERE status='submitted'"):
        task = dict(row)
        submission = store.connection.execute('SELECT * FROM submissions WHERE task_id=? ORDER BY id DESC LIMIT 1',
                                               (task['id'],)).fetchone()
        if not task['error'] and submission is not None and receipts.assess_submission(submission, workflow_key=task['workflow_key']).verified:
            tasks.append(task)
    return tasks


def cleanup(store, *, execute=False, task_id=None):
    try:
        return _cleanup(store, execute=execute, task_id=task_id)
    except (OSError, sqlite3.Error, ValueError) as error:
        return {'status': 'failed', 'error': type(error).__name__}


def _cleanup(store, *, execute, task_id):
    root = store.path.parent.resolve()
    if store.connection.execute("SELECT 1 FROM tasks WHERE status='preparing' LIMIT 1").fetchone():
        return {'status': 'deferred', 'reason': 'collection_in_progress'}
    completed = verified_tasks(store)
    completed_ids = {task['id'] for task in completed}
    protected = set()
    for row in store.connection.execute('SELECT id FROM tasks'):
        if row['id'] not in completed_ids:
            protected.update(path.resolve() for path in task_paths(store, store.get_task(row['id'])))
    candidates = set()
    directories = set()
    for task in completed:
        if task_id is not None and task['id'] != task_id:
            continue
        directory = root / 'materials' / str(task['id'])
        if directory.is_dir() and not directory.is_symlink() and directory.resolve().is_relative_to(root):
            candidates.update(path for path in directory.rglob('*') if path.is_file())
            directories.update(path for path in directory.rglob('*') if path.is_dir())
            directories.add(directory)
        candidates.update(task_paths(store, store.get_task(task['id'])))
    files = []
    for path in candidates:
        resolved = path.resolve()
        if (not path.is_absolute() or path.is_symlink() or not resolved.is_relative_to(root)
                or resolved.parent == root or resolved in protected or not path.is_file()
                or any(part.startswith('.') and part != '.agents' for part in resolved.relative_to(root).parts)
                or (not resolved.is_relative_to(root / 'materials')
                    and not {'attachments', 'prepared', 'recognitions'}.intersection(resolved.relative_to(root).parts[:-1])
                    and path.name not in {'recognition.json', 'observations.json'})
                or path.name.endswith(('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.sqlite', '.sqlite-wal', '.sqlite-shm', '.db', '.db-wal', '.db-shm'))):
            continue
        files.append(path)
    result = {'status': 'completed' if execute else 'preview', 'files': len(files),
              'bytes': sum(path.stat().st_size for path in files), 'failed_files': 0}
    if execute:
        for path in files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                result['failed_files'] += 1
        for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            if not directory.resolve().is_relative_to(root) or directory.is_symlink():
                continue
            try:
                directory.rmdir()
            except OSError:
                pass
        result['expired_tasks'] = 0
        if not result['failed_files']:
            cache = root / 'attachment-cache.sqlite3'
            if cache.is_file():
                with sqlite3.connect(cache) as connection:
                    for task in completed:
                        if task_id is None or task['id'] == task_id:
                            connection.execute('DELETE FROM attachment_files WHERE request_id=? AND flow_key=?',
                                               (task['request_id'], task['workflow_key']))
            if task_id is None:
                result['expired_tasks'] = store.expire_history(datetime.now(timezone.utc) - timedelta(days=365))
        if result['failed_files']:
            result['status'] = 'partial'
    return result
