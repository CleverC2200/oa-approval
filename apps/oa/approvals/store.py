from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from apps.oa.materials.reports import review_contents


MAX_INFLIGHT = 3
AGENT_CLAIM_LIMIT = 5
INFLIGHT_STATUSES = ("preparing", "reviewing", "reviewed", "submitting")


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
                rule_file TEXT, material_json TEXT);
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
                created_at TEXT NOT NULL,
                UNIQUE(task_id,opinion,recognition_json,material_sha256));
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks,
                review_id INTEGER NOT NULL REFERENCES reviews,
                run_id TEXT NOT NULL REFERENCES runs, started_at TEXT NOT NULL,
                completed_at TEXT, status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}');
        """)

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

    def start_run(self, rows, batch_size=None, *, continue_from=None, selection=None):
        if batch_size is not None and (isinstance(batch_size,bool) or not isinstance(batch_size,int) or batch_size < 0):
            raise ValueError("batch_size_invalid")
        run_id, timestamp = uuid4().hex, now()
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self.continuation_options(continue_from)
            stored = {'selection':selection} if selection is not None else {}
            processed = set()
            if continue_from is not None:
                previous = json.loads(self.run(continue_from)['summary_json'])
                processed.update(previous['handoff']['processed_occurrences'])
                stored['continuation'] = {'previous_run_id':continue_from, 'processed_occurrences':sorted(processed)}
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
                if occurrence in processed:
                    continue
                self.connection.execute("""INSERT INTO requests VALUES (?,?,?,?) ON CONFLICT(request_id)
                    DO UPDATE SET workflow_key=excluded.workflow_key,title=excluded.title,last_seen_at=excluded.last_seen_at""",
                    (request_id,workflow,str(row.get("request_name") or ""),timestamp))
                self.connection.execute("""INSERT INTO tasks(request_id,occurrence,operator_id,received_at,workflow_key,
                    last_run_id,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(occurrence) DO UPDATE SET last_run_id=excluded.last_run_id,last_seen_at=excluded.last_seen_at,
                    status=CASE WHEN tasks.status IN ('queued','preparing','reviewing','reviewed','failed','preview')
                    THEN 'queued' ELSE tasks.status END,agent_id=NULL,
                    error=CASE WHEN tasks.status='submitted' THEN 'submitted_pending_confirmation' ELSE tasks.error END""",
                    (request_id,occurrence,operator,received,workflow,run_id,timestamp,timestamp))
            if batch_size is not None:
                task_ids = [task["id"] for task in self.tasks(run_id) if task["status"] == "queued"][:batch_size]
                stored['batch'] = {'size':batch_size, 'task_ids':task_ids}
            self.connection.execute('UPDATE runs SET summary_json=? WHERE id=?', (encoded(stored),run_id))
        return run_id

    def tasks(self, run_id):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM tasks WHERE last_run_id=? ORDER BY julianday(received_at),CAST(request_id AS INTEGER),request_id,id", (run_id,))]

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

    def claim(self, run_id, workflow_key=None, agent_id=None, *, expected_task_id=None, agent_workflow_key=None):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            run = self.run(run_id)
            tasks = self.tasks(run_id)

            def unclaimed(status, reason):
                return {"run_id":run_id,"status":status,"reason":reason,
                        "summary":dict(Counter(task["status"] for task in tasks))}

            if run["status"] != "running":
                return unclaimed("blocked", "run_blocked")
            if (type(expected_task_id) is not int or expected_task_id < 1
                    or not isinstance(agent_workflow_key,str) or not agent_workflow_key.strip()):
                return unclaimed("refused", "claim_guards_required")
            if not isinstance(agent_id,str) or not agent_id.strip():
                return unclaimed("refused", "agent_identity_required")
            busy = [task for task in tasks if task["status"] in INFLIGHT_STATUSES]
            if len(busy) >= MAX_INFLIGHT:
                return unclaimed("waiting", "inflight_limit")
            if any(task["agent_id"] == agent_id for task in busy):
                return unclaimed("waiting", "agent_busy")
            batch = json.loads(run["summary_json"]).get("batch")
            queued = [task for task in tasks if task["status"] == "queued"
                and (batch is None or task["id"] in batch["task_ids"])]
            candidate = next((task for task in queued
                if not workflow_key or task["workflow_key"] == workflow_key), None)
            if candidate is None:
                if queued:
                    return unclaimed("waiting", "workflow_unavailable")
                if busy:
                    return unclaimed("waiting", "in_flight")
                return unclaimed("done", "batch_exhausted" if batch is not None else "queue_exhausted")
            if candidate["id"] != expected_task_id:
                return unclaimed("refused", "expected_task_changed")
            if candidate["workflow_key"] != agent_workflow_key:
                return unclaimed("refused", "agent_workflow_mismatch")
            previous = [task for task in tasks if task["agent_id"] == agent_id]
            if any(task["workflow_key"] != agent_workflow_key for task in previous):
                return unclaimed("refused", "agent_generation_workflow_mismatch")
            if len(previous) >= AGENT_CLAIM_LIMIT:
                return unclaimed("refused", "agent_claim_limit")
            self.connection.execute("UPDATE tasks SET status='preparing',agent_id=? WHERE id=?", (agent_id,candidate["id"]))
            return {"run_id":run_id,"status":"claimed","task":self.get_task(candidate["id"])}

    def fail(self, task_id, error, *, run_id=None):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            task = self.get_task(task_id)
            run_id = task["last_run_id"] if run_id is None else run_id
            if self.run(run_id)["status"] != "running":
                raise ValueError("run_blocked")
            if task["last_run_id"] != run_id:
                raise ValueError("task_run_mismatch")
            if task["status"] not in {"preparing","reviewing"}:
                raise ValueError("task_not_failable")
            self.connection.execute("UPDATE tasks SET status='failed',error=?,failure_count=failure_count+1 WHERE id=?", (error,task_id))
        return self.get_task(task_id)

    def fail_collection(self, task_id, error):
        with self.connection:
            self.connection.execute("UPDATE tasks SET status='failed',error=?,failure_count=failure_count+1 WHERE id=? AND status='preparing'", (error,task_id))
        return self.get_task(task_id)

    def freeze(self, task_id, material, material_file, material_sha256, frozen, rule_file):
        resources = {str(item["resource_id"]):item for item in material.get("resources", [])}
        prepared = material.get("prepared", {})
        for item in prepared.get("images", []) + prepared.get("structured_contents", []):
            rid = str(item.get("resource_id") or "")
            if rid and rid not in resources:
                resources[rid] = {"resource_id":rid,"name":item.get("name") or rid,
                    "parent_resource_id":item.get("parent_resource_id"),"local_readonly_path":item.get("path")}
        with self.connection:
            self.connection.execute("""UPDATE tasks SET material_file=?,material_sha256=?,frozen_json=?,
                material_json=?,rule_file=?,status=CASE WHEN status='preparing' THEN 'reviewing' ELSE status END,
                error=CASE WHEN status='preparing' THEN NULL ELSE error END WHERE id=?""",
                (str(material_file),material_sha256,encoded(frozen),encoded(material),str(rule_file),task_id))
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

    def review(self, task_id, opinion, recognition=None, observations=None):
        task = self.get_task(task_id)
        recognized = encoded(recognition)
        contents = [*review_contents(recognition), *(observations or [])]
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO reviews(task_id,run_id,opinion,recognition_json,material_sha256,material_file,frozen_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (task_id,task["last_run_id"],opinion,recognized,task["material_sha256"],task["material_file"],task["frozen_json"],now()))
            for item in contents:
                attachment = self.connection.execute("SELECT id FROM attachments WHERE task_id=? AND resource_id=? ORDER BY id DESC LIMIT 1",(task_id,str(item["resource_id"]))).fetchone()
                if attachment is None:
                    raise ValueError("observation_resource_unbound")
                self._content(attachment[0],str(item["locator"]),str(item.get("source") or "agent"),str(item["content"]),str(item.get("status") or "read"),item)
            self.connection.execute("UPDATE tasks SET status='reviewed' WHERE id=?", (task_id,))
        return dict(self.connection.execute("SELECT * FROM reviews WHERE task_id=? AND opinion=? AND recognition_json=? AND material_sha256=?",
            (task_id,opinion,recognized,task["material_sha256"])).fetchone())

    def begin_submission(self, task_id, review_id):
        task = self.get_task(task_id)
        with self.connection:
            if task["status"] in {"submitting","submitted","unknown"}:
                raise ValueError("submission_already_started")
            cursor = self.connection.execute("INSERT INTO submissions(task_id,review_id,run_id,started_at,status) VALUES (?,?,?,?,'started')",
                (task_id,review_id,task["last_run_id"],now()))
            self.connection.execute("UPDATE tasks SET status='submitting' WHERE id=?", (task_id,))
        return cursor.lastrowid

    def finish_submission(self, submission_id, status, result):
        with self.connection:
            row = self.connection.execute("SELECT task_id FROM submissions WHERE id=?", (submission_id,)).fetchone()
            self.connection.execute("UPDATE submissions SET status=?,result_json=?,completed_at=? WHERE id=?",(status,encoded(result),now(),submission_id))
            self.connection.execute("UPDATE tasks SET status=?,error=? WHERE id=?",(status,result.get("failure_code"),row[0]))
            if status == "unknown":
                self.connection.execute("UPDATE runs SET status='blocked' WHERE id=(SELECT run_id FROM submissions WHERE id=?)",(submission_id,))

    def set_status(self, task_id, status):
        with self.connection:
            self.connection.execute("UPDATE tasks SET status=? WHERE id=?", (status,task_id))

    def details(self, task_id):
        return {"task":self.get_task(task_id),
            "attachments":[dict(row) for row in self.connection.execute("SELECT * FROM attachments WHERE task_id=? ORDER BY id",(task_id,))],
            "contents":[dict(row) for row in self.connection.execute("SELECT c.* FROM attachment_contents c JOIN attachments a ON a.id=c.attachment_id WHERE a.task_id=? ORDER BY c.id",(task_id,))],
            "reviews":[dict(row) for row in self.connection.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY id",(task_id,))],
            "submissions":[dict(row) for row in self.connection.execute("SELECT * FROM submissions WHERE task_id=? ORDER BY id",(task_id,))]}

    def submissions(self, run_id):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM submissions WHERE run_id=? ORDER BY id", (run_id,))]

    def _handoff(self, run, tasks, stored):
        if run['status'] != 'running' or any(task['status'] in (*INFLIGHT_STATUSES, 'unknown') for task in tasks):
            return {'status':'blocked', 'reason':'continuation_unresolved_tasks'}
        from apps.oa.approvals.status import submission_acceptance

        submitted = [task for task in tasks if task['status'] == 'submitted']
        if submitted and not submission_acceptance(self,run['id'],submitted)['all_verified']:
            return {'status':'blocked', 'reason':'continuation_unverified_submission'}
        remaining = sum(task['status'] == 'queued' for task in tasks)
        if not remaining:
            return {'status':'done', 'remaining':0}
        if not any(task['status'] in {'preview','failed','submitted'} for task in tasks):
            return {'status':'blocked', 'reason':'continuation_no_progress'}
        processed = set(stored.get('continuation', {}).get('processed_occurrences', []))
        processed.update(task['occurrence'] for task in tasks if task['status'] in {'preview','failed','submitted'})
        return {'status':'ready', 'remaining':remaining, 'processed_occurrences':sorted(processed)}

    def close(self, run_id, *, handoff=False):
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            run = self.run(run_id)
            tasks = self.tasks(run_id)
            summary = dict(Counter(task['status'] for task in tasks))
            stored = json.loads(run['summary_json'])
            if handoff:
                stored.setdefault('selection', {'workflows':None, 'request_ids':None,
                                                'batch_size':stored.get('batch', {}).get('size')})
                stored['handoff'] = self._handoff(run,tasks,stored)
            self.connection.execute("UPDATE runs SET status='closed',ended_at=?,summary_json=? WHERE id=?", (now(),encoded({**stored,**summary}),run_id))
        result = {'run_id':run_id, 'status':'closed', 'summary':summary}
        if handoff:
            result['handoff'] = {key:value for key,value in stored['handoff'].items() if key != 'processed_occurrences'}
            if result['handoff']['status'] == 'ready':
                result['handoff'].update(runtime_root=str(self.path.parent.resolve()), previous_run_id=run_id,
                                         start_arguments=['start','--continue-from',run_id,'--limit','0'])
        return result
