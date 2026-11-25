from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
from uuid import uuid4

from apps.oa.materials.reports import review_contents


REVIEW_SLOTS = 3
REVIEW_SLOT_OPTIONS = (REVIEW_SLOTS, 4)
PREPARATION_BUFFER = 1
RESULT_BACKPRESSURE = 3
REVIEW_STATUSES = ("reviewing",)
RESULT_STATUSES = ("reviewed", "submitting")
UNFINISHED_STATUSES = ("preparing", "prepared", *REVIEW_STATUSES, *RESULT_STATUSES)
INVOICE_TITLE_RECHECK = timedelta(minutes=1)


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    def filesystem_value(item):
        if isinstance(item,Path):
            return str(item)
        raise TypeError(f"Unsupported material value: {type(item).__name__}")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,default=filesystem_value)


def occurrence_key(row):
    request_id = str(row.get("request_id") or row.get("requestId") or "").strip()
    operator = str(row.get("operator_id") or "").strip()
    instance = row.get("task_instance_id") or row.get("taskId")
    received = str(row.get("received_at") or "").strip()
    return encoded([request_id,operator,"instance",str(instance)] if instance else [request_id,operator,"received",received])


class Store:
    def __init__(self, path, *, read_only=False):
        self.path = Path(path)
        if read_only:
            self.connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                status TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, workflow_key TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '', last_seen_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests,
                occurrence TEXT NOT NULL UNIQUE, operator_id TEXT NOT NULL,
                received_at TEXT NOT NULL, workflow_key TEXT NOT NULL,
                last_run_id TEXT NOT NULL REFERENCES runs,
                status TEXT NOT NULL DEFAULT 'queued', agent_id TEXT,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                error TEXT, failure_count INTEGER NOT NULL DEFAULT 0,
                material_file TEXT, material_sha256 TEXT, frozen_json TEXT,
                rule_file TEXT, material_json TEXT, recognition_json TEXT);
            CREATE INDEX IF NOT EXISTS task_queue ON tasks(last_run_id,status,received_at);
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks,
                resource_id TEXT NOT NULL, parent_resource_id TEXT,
                name TEXT NOT NULL, path TEXT, sha256 TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE(task_id,resource_id,sha256));
            CREATE TABLE IF NOT EXISTS attachment_contents (
                id INTEGER PRIMARY KEY, attachment_id INTEGER NOT NULL REFERENCES attachments,
                locator TEXT NOT NULL, source TEXT NOT NULL, content TEXT NOT NULL,
                status TEXT NOT NULL, metadata_json TEXT NOT NULL,
                UNIQUE(attachment_id,locator,source,content));
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks,
                run_id TEXT NOT NULL REFERENCES runs, opinion TEXT NOT NULL,
                recognition_json TEXT NOT NULL, material_sha256 TEXT NOT NULL,
                material_file TEXT NOT NULL, frozen_json TEXT NOT NULL,
                created_at TEXT NOT NULL, delivery_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(task_id,opinion,recognition_json,material_sha256));
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks,
                review_id INTEGER NOT NULL REFERENCES reviews,
                run_id TEXT NOT NULL REFERENCES runs, started_at TEXT NOT NULL,
                completed_at TEXT, status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS review_assessments (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks,
                run_id TEXT NOT NULL REFERENCES runs, material_sha256 TEXT NOT NULL,
                assessment_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(task_id,material_sha256,assessment_json));
        """)

        for table, column, definition in (
                ('tasks', 'recognition_json', 'TEXT'),
                ('tasks', 'materials_cleanup_pending', 'INTEGER'),
                ('reviews', 'delivery_json', "TEXT NOT NULL DEFAULT '{}'")):
            if column not in {row['name'] for row in self.connection.execute(f'PRAGMA table_info({table})')}:
                self.connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')

    def require_dispatchable(self, run_id):
        run = self.run(run_id)
        if run['status'] != 'running':
            raise ValueError('run_blocked')
        if self.connection.execute("SELECT 1 FROM tasks WHERE status='unknown'").fetchone() or self.connection.execute(
                "SELECT 1 FROM submissions s JOIN runs r ON r.id=s.run_id WHERE s.status='unknown' OR s.status='started' AND r.status!='running'").fetchone():
            if self.connection.in_transaction:
                self.connection.execute("UPDATE runs SET status='blocked' WHERE id=? AND status='running'", (run_id,))
            else:
                with self.connection:
                    self.connection.execute("UPDATE runs SET status='blocked' WHERE id=? AND status='running'", (run_id,))
            raise ValueError('submission_unresolved')
        return run

    def pipeline_capacity(self, run, tasks):
        selection = json.loads(run['summary_json']).get('selection', {})
        review_limit = selection.get('review_slots', REVIEW_SLOTS)
        result_capacity = RESULT_BACKPRESSURE + review_limit
        reviewing = sum(task['status'] in REVIEW_STATUSES for task in tasks)
        results = sum(task['status'] in RESULT_STATUSES for task in tasks)
        preparation = sum(task['status'] in {'preparing', 'prepared'} for task in tasks)
        return {'reviewing': reviewing, 'review_available': max(0, review_limit - reviewing),
                'review_limit': review_limit, 'preparation': preparation, 'preparation_limit': PREPARATION_BUFFER,
                'results': results, 'result_reserved': reviewing, 'result_capacity': result_capacity,
                'result_backpressure_threshold': RESULT_BACKPRESSURE,
                'backpressure': results >= RESULT_BACKPRESSURE or results + reviewing >= result_capacity}

    def requests(self):
        return [dict(row) for row in self.connection.execute("SELECT * FROM requests ORDER BY request_id")]

    def continuation_options(self, previous_id=None):
        latest = self.latest_run()
        if previous_id is None:
            if latest and json.loads(latest['summary_json']).get('handoff', {}).get('status') == 'ready':
                raise ValueError('continuation_pending')
            return None
        if latest is None or latest['id'] != previous_id or latest['status'] != 'closed':
            raise ValueError('continuation_not_available')
        stored = json.loads(latest['summary_json'])
        if stored.get('handoff', {}).get('status') != 'ready':
            raise ValueError('continuation_not_available')
        return stored['selection']

    def start_run(self, rows, batch_size=None, *, continue_from=None, selection=None, retry_failed=False, retry_reviewed=False,
                  recovery_request_ids=None, pending_count=None, hourly=False):
        if batch_size is not None and (isinstance(batch_size,bool) or not isinstance(batch_size,int) or batch_size < 0):
            raise ValueError("batch_size_invalid")
        run_id, timestamp = uuid4().hex, now()
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self.continuation_options(continue_from)
            stored = {'selection':selection} if selection is not None else {}
            if hourly:
                stored['hourly'] = True
            pending = {'snapshot_at': timestamp, 'raw_count': len(rows) if pending_count is None else pending_count,
                       'selected_count': len(rows), 'outside_selection_count': 0 if pending_count is None else pending_count - len(rows),
                       'excluded': []}
            stored['pending'] = pending
            recovery_tasks = []
            processed = set()
            recovered = set()
            pending_recovery = set()
            latest = self.latest_run()
            if latest is not None:
                previous_summary = json.loads(latest['summary_json'])
                if previous_summary.get('closure'):
                    pending_recovery.update(previous_summary.get('recovery', {}).get('task_ids', []))
            if continue_from is not None:
                previous = json.loads(self.run(continue_from)['summary_json'])
                processed.update(previous['handoff']['processed_occurrences'])
                recovered.update(previous['handoff'].get('recovered_occurrences', []))
                pending_recovery.update(previous.get('recovery', {}).get('task_ids', []))
                stored['continuation'] = {'previous_run_id':continue_from, 'processed_occurrences':sorted(processed),
                                          'recovered_occurrences': sorted(recovered)}
                previous['handoff'].update(status='started', next_run_id=run_id)
                self.connection.execute('UPDATE runs SET summary_json=? WHERE id=?', (encoded(previous),continue_from))
            self.connection.execute("UPDATE runs SET status='interrupted', ended_at=? WHERE status='running'", (timestamp,))
            self.connection.execute("UPDATE tasks SET status='unknown', error='submission_interrupted' WHERE status='submitting'")
            self.connection.execute("UPDATE submissions SET status='unknown' WHERE status='started'")
            self.connection.execute("INSERT INTO runs(id,started_at,status) VALUES (?,?,'running')", (run_id,timestamp))
            for row in rows:
                request_id = str(row.get("request_id") or row.get("requestId") or "").strip()
                operator = str(row.get("operator_id") or "").strip()
                received = str(row.get("received_at") or "").strip()
                workflow = str(row.get("workflow_key") or row.get("flow_key") or "").strip()
                if not request_id or not received or not workflow:
                    raise ValueError("pending_identity_missing")
                occurrence = occurrence_key(row)
                previous_task = self.connection.execute(
                    """SELECT id,status,
                        EXISTS(SELECT 1 FROM reviews WHERE task_id=tasks.id)
                        AND NOT EXISTS(SELECT 1 FROM submissions WHERE task_id=tasks.id) AS unsubmitted_review
                    FROM tasks WHERE occurrence=?""", (occurrence,)).fetchone()
                recovering = (previous_task is not None and request_id in (recovery_request_ids or [])
                              and occurrence not in recovered and (previous_task['status'] == 'failed'
                              or previous_task['status'] == 'queued' and previous_task['id'] in pending_recovery))
                if occurrence in processed and not recovering:
                    excluded = {'request_id': request_id, 'reason': 'continuation_processed'}
                    if previous_task is not None:
                        excluded['task_id'] = previous_task['id']
                    pending['excluded'].append(excluded)
                    continue
                if previous_task and previous_task['status'] == 'failed' and not (retry_failed or recovering):
                    pending['excluded'].append({'task_id': previous_task['id'], 'request_id': request_id, 'reason': 'historical_failure'})
                    continue
                if previous_task and not retry_reviewed and not recovering and (previous_task['status'] == 'preview' or
                        (previous_task['status'] in {'queued', 'reviewed'} and previous_task['unsubmitted_review'])):
                    pending['excluded'].append({'task_id': previous_task['id'], 'request_id': request_id, 'reason': 'unsubmitted_review'})
                    continue
                if recovering or (previous_task is not None and previous_task['id'] in pending_recovery
                                  and previous_task['status'] in {'queued', 'preparing', 'prepared', 'reviewing'}):
                    recovery_tasks.append(previous_task['id'])
                self.connection.execute("""INSERT INTO requests VALUES (?,?,?,?) ON CONFLICT(request_id)
                    DO UPDATE SET workflow_key=excluded.workflow_key,title=excluded.title,last_seen_at=excluded.last_seen_at""",
                    (request_id,workflow,str(row.get("request_name") or ""),timestamp))
                self.connection.execute("""INSERT INTO tasks(request_id,occurrence,operator_id,received_at,workflow_key,
                    last_run_id,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(occurrence) DO UPDATE SET last_run_id=excluded.last_run_id,last_seen_at=excluded.last_seen_at,
                    status=CASE WHEN tasks.status IN ('queued','preparing','prepared','reviewing','reviewed','failed','preview')
                    THEN 'queued' ELSE tasks.status END,agent_id=NULL,
                    error=CASE WHEN tasks.status='submitted' THEN 'submitted_pending_confirmation' ELSE tasks.error END""",
                    (request_id,occurrence,operator,received,workflow,run_id,timestamp,timestamp))
            if batch_size is not None:
                eligible = [task for task in self.tasks(run_id) if task['status'] == 'queued']
                task_ids = [task['id'] for task in eligible][:batch_size]
                stored['batch'] = {'size':batch_size, 'task_ids':task_ids}
            if recovery_request_ids or recovery_tasks:
                stored['recovery'] = {'request_ids': recovery_request_ids or [], 'task_ids': recovery_tasks}
            self.connection.execute('UPDATE runs SET summary_json=? WHERE id=?', (encoded(stored),run_id))
            if self.connection.execute("SELECT 1 FROM tasks WHERE status='unknown'").fetchone():
                self.connection.execute("UPDATE runs SET status='blocked' WHERE id=?", (run_id,))
        return run_id

    def record_travel_reconciliation(self, run_id, receipt):
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET summary_json=json_set(summary_json,'$.travel_reconciliation',json(?)) WHERE id=?",
                (encoded(receipt), run_id))

    def record_cleanup(self, run_id, result):
        self.connection.execute("UPDATE runs SET summary_json=json_set(summary_json,'$.cleanup',json(?)) WHERE id=?",
                                (encoded(result), run_id))

    def tasks(self, run_id):
        tasks = [dict(row) for row in self.connection.execute(
            "SELECT * FROM tasks WHERE last_run_id=? ORDER BY julianday(received_at),CAST(request_id AS INTEGER),request_id,id", (run_id,))]
        return tasks

    def latest_run(self):
        row = self.connection.execute("SELECT * FROM runs ORDER BY started_at DESC,rowid DESC LIMIT 1").fetchone()
        return dict(row) if row is not None else None

    def run(self, run_id):
        row = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("run_not_found")
        return dict(row)

    def get_task(self, task_id):
        row = self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("task_not_found")
        return dict(row)

    def collection_queue(self, run, tasks):
        stored = json.loads(run['summary_json'])
        batch = stored.get('batch')
        queued = [task for task in tasks if task['status'] == 'queued'
                  and (batch is None or task['id'] in batch['task_ids'])]
        pause = stored.get('source_pause')
        if pause is None or pause.get('recovered_at'):
            return queued, pause
        from apps.oa.approvals.sources import invoice_titles

        waiting = [task['id'] for task in queued if task['workflow_key'] in invoice_titles.WORKFLOWS]
        recheck_due = datetime.fromisoformat(now()) >= datetime.fromisoformat(pause['recheck_at'])
        return ([task for task in queued if recheck_due or task['id'] not in waiting],
                {**pause, 'waiting_task_ids': waiting, 'recheck_due': recheck_due})

    def claim(self, run_id, *, expected_task_id=None, authorize=None):
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            tasks = self.tasks(run_id)
            def decision(status, reason):
                return {'run_id': run_id, 'status': status, 'reason': reason,
                        'summary': dict(Counter(task['status'] for task in tasks))}
            try:
                run = self.require_dispatchable(run_id)
            except ValueError as error:
                if str(error) == 'run_not_found':
                    raise
                return decision('blocked', str(error))
            if type(expected_task_id) is not int or expected_task_id < 1:
                return decision('refused', 'claim_guards_required')
            if self.pipeline_capacity(run, tasks)['preparation'] >= PREPARATION_BUFFER:
                return decision('waiting', 'preparation_buffer_full')
            queued, pause = self.collection_queue(run, tasks)
            if not queued:
                if pause and pause.get('waiting_task_ids'):
                    return decision('waiting', 'source_unavailable') | {'source_pause': pause}
                if any(task['status'] in UNFINISHED_STATUSES for task in tasks):
                    return decision('waiting', 'in_flight')
                return decision('done', 'batch_exhausted' if 'batch' in json.loads(run['summary_json']) else 'queue_exhausted')
            candidate = queued[0]
            if candidate['id'] != expected_task_id:
                return decision('refused', 'expected_task_changed')
            authorization = authorize(candidate) if authorize is not None else None
            self.connection.execute("UPDATE tasks SET status='preparing',agent_id=NULL,recognition_json=NULL WHERE id=?",
                                    (candidate['id'],))
            return {'run_id': run_id, 'status': 'claimed', 'task': self.get_task(candidate['id']),
                    **({'authorization': authorization} if authorize is not None else {})}

    def dispatch_review(self, run_id, *, expected_task_id=None, agent_id=None, agent_workflow_key=None, recognition=None):
        from apps.oa.approvals.review import validate_readiness

        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            tasks = self.tasks(run_id)
            def decision(status, reason):
                return {'run_id': run_id, 'status': status, 'reason': reason,
                        'summary': dict(Counter(task['status'] for task in tasks))}
            try:
                run = self.require_dispatchable(run_id)
            except ValueError as error:
                if str(error) == 'run_not_found':
                    raise
                return decision('blocked', str(error))
            if (type(expected_task_id) is not int or expected_task_id < 1
                    or not isinstance(agent_workflow_key, str) or not agent_workflow_key.strip()):
                return decision('refused', 'claim_guards_required')
            if not isinstance(agent_id, str) or not agent_id.strip():
                return decision('refused', 'agent_identity_required')
            capacity = self.pipeline_capacity(run, tasks)
            if not capacity['review_available']:
                return decision('waiting', 'inflight_limit')
            if any(task['agent_id'] == agent_id and task['status'] in REVIEW_STATUSES for task in tasks):
                return decision('waiting', 'agent_busy')
            if capacity['backpressure']:
                return decision('waiting', 'result_backpressure')
            candidate = next((task for task in tasks if task['status'] == 'prepared'), None)
            if candidate is None:
                return decision('waiting', 'materials_not_ready')
            if candidate['id'] != expected_task_id:
                return decision('refused', 'expected_task_changed')
            if candidate['workflow_key'] != agent_workflow_key:
                return decision('refused', 'agent_workflow_mismatch')
            if any(task['agent_id'] == agent_id and task['workflow_key'] != agent_workflow_key for task in tasks):
                return decision('refused', 'agent_generation_workflow_mismatch')
            validate_readiness(candidate, recognition)
            self.connection.execute("UPDATE tasks SET status='reviewing',agent_id=?,recognition_json=? WHERE id=?",
                                    (agent_id, encoded(recognition), candidate['id']))
            return {'run_id': run_id, 'status': 'claimed', 'task': self.get_task(candidate['id'])}

    def fail(self, task_id, error, *, run_id=None, review_assessment=None):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            task = self.get_task(task_id)
            run_id = task["last_run_id"] if run_id is None else run_id
            if self.run(run_id)["status"] != "running":
                raise ValueError("run_blocked")
            if task["last_run_id"] != run_id:
                raise ValueError("task_run_mismatch")
            if task["status"] not in {"preparing","prepared","reviewing"}:
                raise ValueError("task_not_failable")
            if review_assessment is not None:
                from apps.oa.approvals.review import validate_assessment

                assessment = validate_assessment(task, review_assessment)
                if assessment['status'] not in {'incomplete', 'technical_failure'}:
                    raise ValueError('failure_assessment_invalid')
                self._insert_assessment(task, assessment)
            self.connection.execute("UPDATE tasks SET status='failed',error=?,failure_count=failure_count+1 WHERE id=?", (error,task_id))
        return self.get_task(task_id)

    def fail_closed_review(self, run_id, task_id, review_id, reason):
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            latest = self.latest_run()
            task = self.get_task(task_id)
            if latest is None or latest['id'] != run_id or latest['status'] != 'closed':
                raise ValueError('closed_run_mismatch')
            if task['last_run_id'] != run_id or task['status'] != 'reviewed':
                raise ValueError('task_not_unsubmitted_review')
            review = self.connection.execute('SELECT id FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1',
                                             (task_id,)).fetchone()
            if review is None or review['id'] != review_id:
                raise ValueError('review_mismatch')
            if self.connection.execute('SELECT 1 FROM submissions WHERE task_id=?', (task_id,)).fetchone():
                raise ValueError('submission_already_started')
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError('failure_reason_required')
            self.connection.execute("UPDATE tasks SET status='failed',error=?,failure_count=failure_count+1 WHERE id=?",
                                    (reason.strip(), task_id))
        return {'run_id': run_id, 'task_id': task_id, 'review_id': review_id, 'status': 'failed', 'reason': reason.strip()}

    def fail_collection(self, run_id, task_id, error):
        with self.connection:
            self.connection.execute("""UPDATE tasks SET status='failed',error=?,failure_count=failure_count+1
                WHERE id=? AND last_run_id=? AND status='preparing'
                AND EXISTS(SELECT 1 FROM runs WHERE id=last_run_id AND status IN ('running','blocked'))""",
                (error, task_id, run_id))
        return self.get_task(task_id)

    def is_hourly(self, run_id):
        return json.loads(self.run(run_id)['summary_json']).get('hourly', False)

    def pause_invoice_title_source(self, run_id, task_id, report):
        observed_at = now()
        recheck_at = (datetime.fromisoformat(observed_at) + INVOICE_TITLE_RECHECK).isoformat()
        pause = {'source': 'invoice_title_reference', 'observed_at': observed_at, 'recheck_at': recheck_at,
                 'task_id': task_id, 'diagnostics_file': str(report)}
        with self.connection:
            released = self.connection.execute("""UPDATE tasks SET status='queued'
                WHERE id=? AND last_run_id=? AND status='preparing'
                AND EXISTS(SELECT 1 FROM runs WHERE id=last_run_id AND status IN ('running','blocked'))""",
                (task_id, run_id))
            if released.rowcount != 1:
                raise ValueError('collection_run_changed')
            self.connection.execute("UPDATE runs SET summary_json=json_set(summary_json,'$.source_pause',json(?)) WHERE id=?",
                                    (encoded(pause), run_id))
        return pause

    def freeze(self, run_id, task_id, material, material_file, material_sha256, frozen, rule_file, *,
               collection_error=None):
        resources = {str(item["resource_id"]):item for item in material.get("resources", [])}
        prepared = material.get("prepared", {})
        for item in prepared.get("images", []) + prepared.get("structured_contents", []):
            rid = str(item.get("resource_id") or "")
            if rid and rid not in resources:
                resources[rid] = {"resource_id":rid,"name":item.get("name") or rid,
                    "parent_resource_id":item.get("parent_resource_id"),"local_readonly_path":item.get("path")}
        with self.connection:
            frozen_task = self.connection.execute("""UPDATE tasks SET material_file=?,material_sha256=?,frozen_json=?,
                material_json=?,rule_file=?,status=CASE WHEN status='preparing' THEN ? ELSE status END,
                error=CASE WHEN status='preparing' THEN ? ELSE error END,
                failure_count=failure_count+CASE WHEN status='preparing' THEN ? ELSE 0 END
                WHERE id=? AND last_run_id=? AND status IN ('preparing','failed')
                AND EXISTS(SELECT 1 FROM runs WHERE id=last_run_id AND status IN ('running','blocked'))""",
                (str(material_file),material_sha256,encoded(frozen),encoded(material),str(rule_file),
                 'failed' if collection_error is not None else 'prepared',collection_error,
                 int(collection_error is not None),task_id,run_id))
            if frozen_task.rowcount != 1:
                raise ValueError('collection_run_changed')
            if collection_error is None:
                timing = {'material_ready_at': now(), 'material_sha256': material_sha256}
                self.connection.execute("UPDATE runs SET summary_json=json_set(summary_json,?,json(?)) WHERE id=?",
                                        (f'$.artifact_timings."{task_id}"', encoded(timing), run_id))
            if (material.get('invoice_title_reference') or {}).get('status') == 'ready':
                self.connection.execute("""UPDATE runs SET summary_json=json_set(summary_json,'$.source_pause.recovered_at',?)
                    WHERE id=? AND json_type(summary_json,'$.source_pause')='object'
                    AND json_type(summary_json,'$.source_pause.recovered_at') IS NULL""", (now(), run_id))
            ids = {}
            for rid,item in resources.items():
                path = str(item.get("local_readonly_path") or item.get("path") or "")
                digest = frozen.get(path) or str(item.get("source_content_sha256") or "")
                self.connection.execute("""INSERT INTO attachments(task_id,resource_id,parent_resource_id,name,path,sha256,metadata_json)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(task_id,resource_id,sha256) DO UPDATE SET path=excluded.path,metadata_json=excluded.metadata_json""",
                    (task_id,rid,item.get("parent_resource_id"),str(item.get("name") or rid),path,digest,encoded(item)))
                ids[rid] = self.connection.execute("SELECT id FROM attachments WHERE task_id=? AND resource_id=? AND sha256=?",(task_id,rid,digest)).fetchone()[0]
            for index,item in enumerate(prepared.get("structured_contents", [])):
                rid = str(item.get("resource_id") or "")
                if rid in ids:
                    self._content(ids[rid],str(item.get("sheet") or item.get("sheet_name") or f"structured:{index}"),"parser",encoded(item),"read",item)
            for index,item in enumerate(prepared.get("images", [])):
                rid = str(item.get("resource_id") or "")
                if rid in ids:
                    self._content(ids[rid],str(item.get("kind") or f"page:{index+1}"),"image","","prepared",item)

        return self.get_task(task_id)

    def _content(self, attachment_id, locator, source, content, status, metadata):
        self.connection.execute("INSERT OR IGNORE INTO attachment_contents(attachment_id,locator,source,content,status,metadata_json) VALUES (?,?,?,?,?,?)",
            (attachment_id,locator,source,content,status,encoded(metadata)))

    def deliver(self, task_id, *, agent_id, material_file, material_sha256, opinion, assessment, recognition=None, observations=None):
        from apps.oa.approvals.review import validate_assessment, validate_delivery_identity, verify_saved_delivery

        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            task = self.get_task(task_id)
            self.require_dispatchable(task['last_run_id'])
            if task['status'] in {'reviewed', 'preview'}:
                review = self.latest_review(task_id)
                saved = verify_saved_delivery(review)
                normalized = validate_assessment(task, assessment)
                same_assessment = ({key: value for key, value in normalized.items() if key not in {'evidence_files', 'evidence_sha256'}}
                    == {key: value for key, value in saved['assessment'].items() if key not in {'evidence_files', 'evidence_sha256'}}
                    and [normalized['evidence_sha256'][name] for name in normalized['evidence_files']]
                    == [saved['assessment']['evidence_sha256'][name] for name in saved['assessment']['evidence_files']])
                if (saved['agent_id'] != agent_id or saved['material_file'] != material_file
                        or saved['material_sha256'] != material_sha256 or review['opinion'] != opinion
                        or review['recognition_json'] != encoded(recognition) or not same_assessment
                        or saved['observations_sha256'] != hashlib.sha256(encoded(observations).encode()).hexdigest()):
                    raise ValueError('review_delivery_changed')
                return review
            validate_delivery_identity(task, agent_id, material_file, material_sha256)
            assessment = validate_assessment(task, assessment)
            if assessment['status'] not in {'complete', 'business_pending'}:
                raise ValueError('review_not_complete')
            if not isinstance(opinion, str) or not opinion.strip():
                raise ValueError('opinion_required')
            if any(item['run_id'] == task['last_run_id'] and item['material_sha256'] == material_sha256
                    and item['assessment']['status'] in {'technical_failure', 'incomplete'}
                    for item in self.review_assessments(task_id)):
                raise ValueError('review_failure_unresolved')
            self._insert_assessment(task, assessment)
            recognized = encoded(recognition)
            delivery = {'agent_id': agent_id, 'task_id': task_id, 'material_file': material_file,
                        'material_sha256': material_sha256, 'assessment': assessment,
                        'observations_sha256': hashlib.sha256(encoded(observations).encode()).hexdigest()}
            self.connection.execute("INSERT INTO reviews(task_id,run_id,opinion,recognition_json,material_sha256,material_file,frozen_json,created_at,delivery_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, task['last_run_id'], opinion, recognized, material_sha256, material_file, task['frozen_json'], now(), encoded(delivery)))
            review_id = self.connection.execute('SELECT last_insert_rowid()').fetchone()[0]
            if isinstance(recognition, dict):
                timing = {key: recognition[key] for key in ('status', 'created_at', 'completed_at')
                          if isinstance(recognition.get(key), str) and len(recognition[key]) <= 64}
                self.connection.execute("UPDATE runs SET summary_json=json_set(summary_json,?,json(?)) WHERE id=?",
                                        (f'$.artifact_timings."{task_id}".recognition', encoded(timing), task['last_run_id']))
            for item in [*review_contents(recognition), *(observations or [])]:
                attachment = self.connection.execute('SELECT id FROM attachments WHERE task_id=? AND resource_id=? ORDER BY id DESC LIMIT 1',
                                                     (task_id, str(item['resource_id']))).fetchone()
                if attachment is None:
                    raise ValueError('observation_resource_unbound')
                self._content(attachment[0], str(item['locator']), str(item.get('source') or 'agent'), str(item['content']),
                              str(item.get('status') or 'read'), item)
            self.connection.execute("UPDATE tasks SET status='reviewed' WHERE id=?", (task_id,))
        return dict(self.connection.execute('SELECT * FROM reviews WHERE id=?', (review_id,)).fetchone())

    def latest_review(self, task_id):
        row = self.connection.execute('SELECT * FROM reviews WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,)).fetchone()
        if row is None:
            raise ValueError('review_delivery_required')
        return dict(row)

    def begin_submission(self, task_id, review_id, *, result=None):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            task = self.get_task(task_id)
            self.require_dispatchable(task["last_run_id"])
            if self.connection.execute("SELECT 1 FROM submissions WHERE status='started'").fetchone():
                raise ValueError("writer_busy")
            if task["status"] in {"submitting","submitted","unknown"}:
                raise ValueError("submission_already_started")
            if task['status'] not in {'reviewed', 'preview'}:
                raise ValueError('task_not_reviewable')
            review = self.latest_review(task_id)
            if (review['id'] != review_id or review['run_id'] != task['last_run_id']
                    or review['material_sha256'] != task['material_sha256']):
                raise ValueError('review_mismatch')
            cursor = self.connection.execute("INSERT INTO submissions(task_id,review_id,run_id,started_at,status,result_json) VALUES (?,?,?,?,'started',?)",
                (task_id,review_id,task["last_run_id"],now(),encoded(result or {})))
            self.connection.execute("UPDATE tasks SET status='submitting' WHERE id=?", (task_id,))
        return cursor.lastrowid

    def finish_submission(self, submission_id, status, result):
        with self.connection:
            row = self.connection.execute("SELECT task_id FROM submissions WHERE id=?", (submission_id,)).fetchone()
            self.connection.execute("UPDATE submissions SET status=?,result_json=?,completed_at=? WHERE id=?",(status,encoded(result),now(),submission_id))
            self.connection.execute("UPDATE tasks SET status=?,error=? WHERE id=?",(status,result.get("failure_code"),row[0]))
            if status == "unknown":
                self.connection.execute(
                    "UPDATE runs SET status='blocked' WHERE id=(SELECT run_id FROM submissions WHERE id=?) AND ended_at IS NULL",
                    (submission_id,))
            elif status == "submitted":
                self.connection.execute('UPDATE tasks SET materials_cleanup_pending=1 WHERE id=? AND materials_cleanup_pending IS NULL', (row[0],))
                self.connection.execute("""UPDATE runs SET status='closed'
                    WHERE id=(SELECT run_id FROM submissions WHERE id=?)
                    AND status='blocked' AND ended_at IS NOT NULL AND json_type(summary_json,'$.execution')='object'""",
                    (submission_id,))

    def set_status(self, task_id, status):
        with self.connection:
            self.connection.execute("UPDATE tasks SET status=? WHERE id=?", (status,task_id))

    def details(self, task_id):
        return {"task":self.get_task(task_id),
            "review_assessments":self.review_assessments(task_id),
            "attachments":[dict(row) for row in self.connection.execute("SELECT * FROM attachments WHERE task_id=? ORDER BY id",(task_id,))],
            "contents":[dict(row) for row in self.connection.execute("SELECT c.* FROM attachment_contents c JOIN attachments a ON a.id=c.attachment_id WHERE a.task_id=? ORDER BY c.id",(task_id,))],
            "reviews":[dict(row) for row in self.connection.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY id",(task_id,))],
            "submissions":[dict(row) for row in self.connection.execute("SELECT * FROM submissions WHERE task_id=? ORDER BY id",(task_id,))]}

    def unresolved_writes(self):
        return [dict(row) for row in self.connection.execute(
            """SELECT t.id AS task_id,t.last_run_id AS run_id,t.status AS task_status,
                s.id AS submission_id,s.run_id AS submission_run_id,s.status AS submission_status
                FROM tasks t LEFT JOIN submissions s ON s.task_id=t.id AND s.status IN ('started','unknown')
                WHERE t.status IN ('unknown','submitting') OR s.id IS NOT NULL
                ORDER BY t.id,s.id""")]

    def submissions(self, run_id):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM submissions WHERE run_id=? ORDER BY id", (run_id,))]

    def assess_review(self, task, assessment):
        self.require_dispatchable(task['last_run_id'])
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            current = self.get_task(task['id'])
            self.require_dispatchable(current['last_run_id'])
            if current['status'] != 'reviewing':
                raise ValueError('task_not_reviewable')
            if current['last_run_id'] != task['last_run_id'] or current['material_sha256'] != task['material_sha256']:
                raise ValueError('review_material_mismatch')
            self._insert_assessment(current, assessment)

    def _insert_assessment(self, task, assessment):
        self.connection.execute('INSERT OR IGNORE INTO review_assessments(task_id,run_id,material_sha256,assessment_json,created_at) VALUES (?,?,?,?,?)',
            (task['id'], task['last_run_id'], task['material_sha256'], encoded(assessment), now()))

    def review_assessments(self, task_id):
        if not self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_assessments'").fetchone():
            return []
        return [{key: value for key, value in dict(row).items() if key != 'assessment_json'}
                | {'assessment': json.loads(row['assessment_json'])}
                for row in self.connection.execute('SELECT * FROM review_assessments WHERE task_id=? ORDER BY id', (task_id,))]

    def _handoff(self, run, tasks, stored):
        if run['status'] != 'running' or any(task['status'] in (*UNFINISHED_STATUSES, 'unknown') for task in tasks):
            return {'status':'blocked', 'reason':'continuation_unresolved_tasks'}
        from apps.oa.approvals.status import submission_acceptance

        submitted = [task for task in tasks if task['status'] == 'submitted']
        submitted_facts = submission_acceptance(self, run['id'], submitted)['tasks']
        if any(not task['occurrence_verified'] or any(not attempt['verified'] for attempt in task['submissions'])
               for task in submitted_facts):
            return {'status':'blocked', 'reason':'continuation_unverified_submission'}
        remaining = sum(task['status'] == 'queued' for task in tasks)
        if not remaining:
            return {'status':'done', 'remaining':0}
        stop = stored['execution']['stop'] or {}
        if not (stop.get('kind') == 'source_unavailable'
                or any(task['status'] in {'preview', 'failed'} for task in tasks)
                or any(task['verified_attempt_count'] for task in submitted_facts)):
            return {'status':'blocked', 'reason':'continuation_no_progress'}
        processed = set(stored.get('continuation', {}).get('processed_occurrences', []))
        processed.update(task['occurrence'] for task in tasks if task['status'] in {'preview','failed','submitted'})
        recovered = set(stored.get('continuation', {}).get('recovered_occurrences', []))
        recovery_tasks = stored.get('recovery', {}).get('task_ids', [])
        recovered.update(task['occurrence'] for task in tasks if task['id'] in recovery_tasks
                         and task['status'] in {'preview', 'failed', 'submitted'})
        return {'status':'ready', 'remaining':remaining, 'processed_occurrences':sorted(processed),
                'recovered_occurrences': sorted(recovered)}

    def close(self, run_id, *, handoff=False, collection_stopped_note=None, stop=None, closure=None):
        from apps.oa.approvals.execution import close_execution, validate_stop
        from apps.oa.approvals.retention import pending_cleanup

        stop = validate_stop(self, run_id, stop, handoff=handoff)
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            run = self.run(run_id)
            tasks = self.tasks(run_id)
            summary = dict(Counter(task['status'] for task in tasks))
            stored = json.loads(run['summary_json'])
            frozen = stored.get('batch')
            if 'closed_batch' not in stored:
                scoped = tasks if frozen is None else [self.get_task(task_id) for task_id in frozen['task_ids']]
                stored['closed_batch'] = {
                    'summary': dict(Counter(task['status'] for task in scoped)),
                    'unfinished': [{'task_id': task['id'], 'status': task['status'],
                                    'material_file': task['material_file']}
                                   for task in scoped if task['status'] in UNFINISHED_STATUSES],
                }
                for unfinished in stored['closed_batch']['unfinished']:
                    if unfinished['status'] == 'preparing':
                        directory = self.path.parent / 'materials' / str(unfinished['task_id'])
                        if directory.is_dir() and not directory.is_symlink():
                            unfinished['partial_directories'] = [str(child) for child in sorted(directory.iterdir())
                                                                if child.is_dir() and not child.is_symlink()]
            if closure is not None:
                stored.setdefault('closure', {**closure, 'recorded_at': now()})
            if run['status'] == 'closed':
                if closure is not None:
                    if 'execution' in stored:
                        stored['execution']['archive_eligible'] = False
                    self.connection.execute('UPDATE runs SET summary_json=? WHERE id=?', (encoded(stored), run_id))
                return {'run_id': run_id, 'status': 'closed', 'summary': summary,
                        **{key: stored[key] for key in ('execution', 'closure', 'cleanup') if key in stored}}
            pending = pending_cleanup(self, run_id=run_id, default='awaiting_cleanup_sweep')
            if pending:
                stored['cleanup'] = {'status': 'deferred', 'reason': 'awaiting_cleanup_sweep', 'pending_tasks': pending}
            stored['execution'] = close_execution(self, run, tasks, stop)
            if closure is not None:
                stored['execution']['archive_eligible'] = False
            if collection_stopped_note:
                stored['orphaned_collection'] = {
                    'confirmation_note': collection_stopped_note,
                    'task_ids': [task['id'] for task in tasks if task['status'] == 'preparing'],
                }
            if handoff:
                stored.setdefault('selection', {'workflows':None, 'request_ids':None,
                                                'batch_size':stored.get('batch', {}).get('size')})
                stored['handoff'] = self._handoff(run,tasks,stored)
                if stored['handoff']['status'] == 'blocked':
                    stored['execution'].update(status='blocked', archive_eligible=False)
                elif stored['handoff']['status'] == 'ready' and stop and stop['kind'] == 'authorized_handoff':
                    stored['execution']['handoff_remaining_count'] = stored['handoff']['remaining']
            self.connection.execute("UPDATE runs SET status='closed',ended_at=?,summary_json=? WHERE id=?", (now(),encoded({**stored,**summary}),run_id))
        result = {'run_id':run_id, 'status':'closed', 'summary':summary, 'execution':stored['execution']}
        if 'cleanup' in stored:
            result['cleanup'] = stored['cleanup']
        if closure is not None:
            result['closure'] = stored['closure']
        if handoff:
            result['handoff'] = {key:value for key,value in stored['handoff'].items()
                                 if key not in {'processed_occurrences', 'recovered_occurrences'}}
            if result['handoff']['status'] == 'ready':
                result['handoff'].update(runtime_root=str(self.path.parent.resolve()), previous_run_id=run_id,
                                         start_arguments=['start','--continue-from',run_id,'--limit','0'])
        return result

    def expire_history(self, cutoff):
        from apps.oa.approvals.retention import verified_tasks

        timestamp = cutoff.isoformat()
        expired = []
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            for task in verified_tasks(self):
                last_submission = self.connection.execute(
                    'SELECT MAX(completed_at) FROM submissions WHERE task_id=?', (task['id'],)).fetchone()[0]
                run = self.run(task['last_run_id'])
                if (task['last_seen_at'] < timestamp and last_submission < timestamp
                        and run['status'] in {'closed', 'interrupted'}):
                    expired.append(task['id'])
            for task_id in expired:
                self.connection.execute('DELETE FROM review_assessments WHERE task_id=?', (task_id,))
                self.connection.execute('DELETE FROM submissions WHERE task_id=?', (task_id,))
                self.connection.execute('DELETE FROM reviews WHERE task_id=?', (task_id,))
                self.connection.execute('DELETE FROM attachment_contents WHERE attachment_id IN (SELECT id FROM attachments WHERE task_id=?)', (task_id,))
                self.connection.execute('DELETE FROM attachments WHERE task_id=?', (task_id,))
                self.connection.execute('DELETE FROM tasks WHERE id=?', (task_id,))
            self.connection.execute('DELETE FROM requests WHERE last_seen_at < ? AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.request_id=requests.request_id)', (timestamp,))
            latest = self.latest_run()
            if latest:
                self.connection.execute("""DELETE FROM runs WHERE id != ? AND ended_at < ?
                    AND status IN ('closed','interrupted')
                    AND COALESCE(json_extract(summary_json,'$.handoff.status'),'') != 'ready'
                    AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.last_run_id=runs.id)
                    AND NOT EXISTS (SELECT 1 FROM reviews WHERE reviews.run_id=runs.id)
                    AND NOT EXISTS (SELECT 1 FROM review_assessments WHERE review_assessments.run_id=runs.id)
                    AND NOT EXISTS (SELECT 1 FROM submissions WHERE submissions.run_id=runs.id)""", (latest['id'],timestamp))
        return len(expired)
