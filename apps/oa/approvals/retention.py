from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import stat
from pathlib import Path

from apps.oa.approvals import receipts
from apps.oa.approvals.collection import cache as attachment_cache


PATH_FIELDS = {'path', 'source_path', 'local_readonly_path', 'source_image_path',
               'material_file', 'rule_file', 'report_file', 'observations_file', 'diagnostics_file'}
ASSESSMENT_FIELDS = ('status', 'reason', 'rule_basis')


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
    for field in ('material_json', 'recognition_json'):
        paths.update(referenced_paths(json.loads(task.get(field) or '{}')))
    for row in store.connection.execute('SELECT path FROM attachments WHERE task_id=? AND path IS NOT NULL', (task['id'],)):
        paths.add(Path(row['path']))
    for row in store.connection.execute("""SELECT json_extract(content.metadata_json,'$.report_file') AS report_file
            FROM attachment_contents content JOIN attachments a ON a.id=content.attachment_id
            WHERE a.task_id=? AND json_type(content.metadata_json,'$.report_file')='text'""", (task['id'],)):
        report = Path(row['report_file'])
        paths.add(report)
        if report.name == 'recognition.json':
            paths.add(report.with_name('observations.json'))
    for row in store.connection.execute('SELECT * FROM reviews WHERE task_id=?', (task['id'],)):
        review = dict(row)
        paths.update(Path(path) for path in json.loads(review['frozen_json']))
        for field in ('recognition_json', 'delivery_json'):
            paths.update(referenced_paths(json.loads(review.get(field) or '{}')))
    for entry in store.review_assessments(task['id']):
        paths.update(Path(path) for path in entry['assessment'].get('evidence_files', ()))
    return {path for path in paths if path.is_absolute()}


def verified_tasks(store):
    tasks = []
    for row in store.connection.execute("""SELECT id,request_id,workflow_key,last_run_id,last_seen_at,error FROM tasks
            WHERE status='submitted' AND materials_cleanup_pending=0 AND failure_count=0
            AND NOT EXISTS (SELECT 1 FROM submissions WHERE task_id=tasks.id AND status='failed')"""):
        task = dict(row)
        assessments = store.review_assessments(task['id'])
        if not assessments or any(item['assessment']['status'] != 'complete' for item in assessments):
            continue
        submission = store.connection.execute('SELECT * FROM submissions WHERE task_id=? ORDER BY id DESC LIMIT 1',
                                               (task['id'],)).fetchone()
        if not task['error'] and submission is not None and receipts.assess_submission(submission, workflow_key=task['workflow_key']).verified:
            tasks.append(task)
    return tasks


def _completed_tasks(store, task_id, history):
    connection = store.connection
    has_pending_column = any(row['name'] == 'materials_cleanup_pending' for row in connection.execute('PRAGMA table_info(tasks)'))
    if not history and not has_pending_column:
        return {}
    pending = 't.materials_cleanup_pending' if has_pending_column else 'NULL'
    scope = f'{pending} IS NOT 0' if history else f'{pending}=1'
    delivery = ('r.delivery_json' if any(row['name'] == 'delivery_json'
                for row in connection.execute('PRAGMA table_info(reviews)')) else "'{}'")
    rows = connection.execute(f"""SELECT t.id AS task_id,t.request_id,t.workflow_key,t.failure_count,
        s.id,s.review_id,s.status,s.completed_at,s.result_json,r.opinion,{delivery} AS delivery_json,r.material_sha256,r.run_id,
        EXISTS(SELECT 1 FROM submissions AS attempt WHERE attempt.task_id=t.id AND attempt.status='failed') AS failed_attempt
        FROM tasks t JOIN submissions s ON s.id=(SELECT MAX(id) FROM submissions WHERE task_id=t.id)
        JOIN reviews r ON r.id=s.review_id AND r.task_id=t.id
        WHERE t.status='submitted' AND COALESCE(t.error,'')='' AND {scope} AND (? IS NULL OR t.id=?) ORDER BY t.id""", (task_id, task_id))
    return {row['task_id']: dict(row) for row in rows if row['opinion'].strip()
            and receipts.assess_submission(row, workflow_key=row['workflow_key']).verified}


def _assessment(store, submission):
    assessment = json.loads(submission['delivery_json']).get('assessment')
    if not assessment:
        assessment = next((item['assessment'] for item in reversed(store.review_assessments(submission['task_id']))
                           if item['run_id'] == submission['run_id']
                           and item['material_sha256'] == submission['material_sha256']
                           and item['assessment']['status'] in {'complete', 'business_pending'}), {})
    if assessment.get('status') == 'complete':
        return {'status': 'complete'}
    return {key: assessment[key] for key in ASSESSMENT_FIELDS if key in assessment}


def _compact(store, submission, assessment):
    connection = store.connection
    task_id = submission['task_id']
    connection.execute('DELETE FROM attachment_contents WHERE attachment_id IN (SELECT id FROM attachments WHERE task_id=?)', (task_id,))
    connection.execute('DELETE FROM attachments WHERE task_id=?', (task_id,))
    connection.execute('DELETE FROM reviews WHERE task_id=? AND id NOT IN (SELECT review_id FROM submissions WHERE task_id=?)',
                       (task_id, task_id))
    connection.execute("""UPDATE reviews SET frozen_json='{}',material_file='',
        recognition_json=CASE WHEN id=? THEN '{}' ELSE json_object('review_id',id) END,
        delivery_json=CASE WHEN id=? THEN ? ELSE '{}' END WHERE task_id=?""",
                       (submission['review_id'], submission['review_id'],
                        json.dumps({'assessment': assessment}) if assessment else '{}', task_id))
    connection.execute('DELETE FROM review_assessments WHERE task_id=?', (task_id,))
    if assessment:
        connection.execute("""INSERT INTO review_assessments(task_id,run_id,material_sha256,assessment_json,created_at)
            VALUES (?,?,?,?,?)""", (task_id, submission['run_id'], submission['material_sha256'],
                                  json.dumps(assessment), submission['completed_at']))
    connection.execute("""UPDATE tasks SET material_file=NULL,rule_file=NULL,material_json=NULL,
        frozen_json=NULL,recognition_json=NULL,materials_cleanup_pending=0 WHERE id=?""", (task_id,))


def _older_collection_states(root, task_ids):
    states = {}
    if not task_ids:
        return states
    for path in (root / 'collection-states').glob('*.read.json'):
        if path.resolve() != path:
            continue
        try:
            with path.open() as stream:
                record = json.load(stream)
        except (FileNotFoundError, ValueError):
            continue
        task_id = record.get('task_id') if isinstance(record, dict) else None
        if type(task_id) is int and task_id in task_ids:
            states.setdefault(task_id, set()).update((path, path.with_name(path.name[:-10] + '.json')))
    return states


def pending_cleanup(store, *, task_id=None, run_id=None, reasons=None, default='cleanup_pending'):
    reasons = reasons or {}
    return [{'task_id': row['id'], 'reason': reasons.get(row['id']) or
             ('submission_confirmation_required' if row['error'] else default)}
            for row in store.connection.execute("""
                SELECT id,error FROM tasks WHERE materials_cleanup_pending=1
                AND (? IS NULL OR id=?) AND (? IS NULL OR last_run_id=?) ORDER BY id
            """, (task_id, task_id, run_id, run_id))]


def _report_pending(store, result, task_id):
    reasons = {item['task_id']: item['reason'] for item in result.pop('pending_tasks', [])}
    pending = pending_cleanup(store, task_id=task_id, reasons=reasons, default=result.get('reason', 'cleanup_pending'))
    if pending:
        result['pending_tasks'] = pending
        if result['status'] == 'completed':
            result['status'] = 'deferred'


def cleanup(store, *, execute=False, task_id=None, history=False, closed_run_id=None):
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    try:
        with store.connection:
            store.connection.execute('BEGIN IMMEDIATE' if execute else 'BEGIN')
            result = _cleanup(store, execute=execute, task_id=task_id, history=history, cutoff=cutoff)
            expire = execute and result['status'] == 'completed' and task_id is None and not history
            if execute and not history:
                _report_pending(store, result, task_id)
                if closed_run_id is not None:
                    store.record_cleanup(closed_run_id, result)
        if expire:
            result['expired_tasks'] = store.expire_history(cutoff)
        return result
    except (OSError, sqlite3.Error, ValueError) as error:
        result = {'status': 'failed', 'error': type(error).__name__, 'reason': 'cleanup_failed'}
        if execute and not history:
            try:
                with store.connection:
                    _report_pending(store, result, task_id)
                    if closed_run_id is not None:
                        store.record_cleanup(closed_run_id, result)
            except sqlite3.Error:
                pass
        return result


def _cleanup(store, *, execute, task_id, history, cutoff):
    root = store.path.parent.resolve()
    collecting = store.connection.execute("""SELECT 1 FROM tasks t JOIN runs r ON r.id=t.last_run_id
        WHERE t.status='preparing' AND r.status IN ('running','blocked') LIMIT 1""").fetchone() is not None
    if execute and collecting:
        return {'status': 'deferred', 'reason': 'collection_in_progress'}
    result = {'status': 'completed' if execute else 'preview', 'files': 0, 'bytes': 0,
              'allocated_bytes': 0, 'failed_files': 0, 'tasks': 0, 'protected_tasks': 0}
    if history and not execute:
        result.update(root=str(root), items=[], collection_in_progress=collecting)
    completed = _completed_tasks(store, task_id, history)
    if not completed:
        return result

    closed_materials = set()
    for row in store.connection.execute(
            """SELECT scoped.value FROM runs, json_each(runs.summary_json, '$.closed_batch.unfinished') AS scoped
            WHERE ended_at >= ? AND json_extract(runs.summary_json, '$.closure.kind') IS NOT NULL""", (cutoff.isoformat(),)):
        unfinished = json.loads(row['value'])
        if unfinished['task_id'] in completed:
            continue
        if unfinished.get('material_file'):
            closed_materials.add(Path(unfinished['material_file']).parent.resolve())
        closed_materials.update(Path(directory).resolve() for directory in unfinished.get('partial_directories', []))

    protected = set()
    active_scopes = set()
    for row in store.connection.execute("""SELECT t.id AS task_id,t.request_id,t.workflow_key,t.status AS task_status,t.error,
            s.status,s.completed_at,s.result_json FROM tasks t
            LEFT JOIN submissions s ON s.id=(SELECT MAX(id) FROM submissions WHERE task_id=t.id)"""):
        if (row['task_status'] == 'submitted' and not row['error'] and row['status'] is not None
                and receipts.assess_submission(row, workflow_key=row['workflow_key']).verified):
            continue
        active_scopes.add((row['request_id'], row['workflow_key']))
        protected.update(path.resolve() for path in task_paths(store, store.get_task(row['task_id'])))

    collect_states = root / 'collection-states'
    old_states = _older_collection_states(root, {task_id for task_id, submission in completed.items()
                                                if history or submission['failure_count']})
    with attachment_cache.runtime_root(root):
        configured_cache = attachment_cache.db_path().resolve() if attachment_cache.is_enabled() else None
    caches = {root / 'attachment-cache.sqlite3', configured_cache} - {None, store.path.resolve()}
    seen = set()
    for task_id, submission in completed.items():
        directory = root / 'materials' / str(task_id)
        candidates = task_paths(store, store.get_task(task_id))
        directories = []
        if directory.resolve() == directory and directory.is_dir():
            for path in directory.rglob('*'):
                if path.is_dir() and not path.is_symlink():
                    directories.append(path)
                else:
                    candidates.add(path)
            directories.append(directory)
        checkpoints = collect_states / str(task_id)
        candidates.update(checkpoints.glob('*.json'))
        candidates.update(old_states.get(task_id, ()))
        if checkpoints.resolve() == checkpoints and checkpoints.is_dir():
            directories.append(checkpoints)
        diagnostics = root / 'collection-errors'
        candidates.update(diagnostics.glob(f'*/{task_id}.json'))
        candidates.update(diagnostics.glob(f'*/{task_id}/*.json'))

        assessment = _assessment(store, submission)
        files = {}
        for path in sorted(candidates):
            if not path.is_relative_to(root) or path.resolve() != path:
                continue
            parts = path.relative_to(root).parts
            if (len(parts) < 2 or any(part.startswith('.') and part != '.agents' for part in parts)
                    or (parts[0] == 'materials' and not path.is_relative_to(directory))
                    or (parts[0] not in {'materials', 'collection-errors', 'collection-states', 'review-errors'}
                        and not {'attachments', 'prepared', 'recognitions'}.intersection(parts[:-1])
                        and path.name not in {'recognition.json', 'observations.json'})
                    or path.name.endswith(('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.sqlite', '.sqlite-wal',
                                           '.sqlite-shm', '.db', '.db-wal', '.db-shm'))):
                continue
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode):
                files[path] = info

        if any(path in protected or any(parent in closed_materials for parent in path.parents) for path in files):
            result['protected_tasks'] += 1
            result.setdefault('pending_tasks', []).append({'task_id': task_id, 'reason': 'shared_material_in_use'})
            if 'items' in result:
                result['items'].append({'task_id': task_id, 'request_id': submission['request_id'],
                                        'status': 'shared_material_in_use'})
            continue
        fresh = files.keys() - seen
        seen.update(fresh)
        size = sum(files[path].st_size for path in fresh)
        allocated = sum(files[path].st_blocks * 512 for path in fresh if files[path].st_nlink == 1)
        result['files'] += len(fresh)
        result['bytes'] += size
        result['allocated_bytes'] += allocated
        result['tasks'] += 1
        if 'items' in result:
            result['items'].append({'task_id': task_id, 'request_id': submission['request_id'],
                'workflow_key': submission['workflow_key'], 'submission_id': submission['id'],
                'business_pending': assessment.get('status') == 'business_pending',
                'retried': bool(submission['failure_count'] or submission['failed_attempt']),
                'files': [str(path.relative_to(root)) for path in sorted(files)], 'allocated_bytes': allocated})
        if not execute:
            continue
        failures = 0
        for path in files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                failures += 1
        result['failed_files'] += failures
        if failures:
            result.setdefault('pending_tasks', []).append({'task_id': task_id, 'reason': 'file_cleanup_failed'})
            continue
        for path in sorted(directories, key=lambda path: len(path.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
        scope = (submission['request_id'], submission['workflow_key'])
        if scope not in active_scopes:
            for cache in caches:
                if cache.is_relative_to(root) and cache.resolve() == cache and cache.is_file():
                    with sqlite3.connect(cache) as connection:
                        connection.execute('DELETE FROM attachment_files WHERE request_id=? AND flow_key=?', scope)
        _compact(store, submission, assessment)

    if result['failed_files']:
        result['status'] = 'partial'
    elif execute and result['protected_tasks']:
        result['status'] = 'deferred'
    return result
