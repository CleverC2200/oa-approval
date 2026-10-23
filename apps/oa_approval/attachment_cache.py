from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from typing import Any


DEFAULT_DB_PATH = Path('runtime') / 'attachment-cache.sqlite3'


def is_enabled() -> bool:
    return os.environ.get('OA_FINANCE_PREAUDIT_CACHE_DB', '').strip().lower() not in {'0', 'false', 'off', 'no'}


def db_path() -> Path:
    configured = os.environ.get('OA_FINANCE_PREAUDIT_CACHE_DB', '').strip()
    return Path(configured).expanduser() if configured else DEFAULT_DB_PATH


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    path.chmod(0o600)
    connection.execute('''CREATE TABLE IF NOT EXISTS attachment_files (
        request_id TEXT NOT NULL, flow_key TEXT NOT NULL, resource_key TEXT NOT NULL,
        entry_json TEXT NOT NULL, PRIMARY KEY(request_id, flow_key, resource_key))''')
    return connection


def record_attachment_file(*, request_id: str, flow_key: str, resource_key: str,
                           entry_json: dict[str, Any]) -> dict[str, Any]:
    if not is_enabled():
        return {'enabled': False, 'ok': True}
    try:
        with _connect() as connection:
            connection.execute('''INSERT INTO attachment_files VALUES (?, ?, ?, ?)
                ON CONFLICT(request_id, flow_key, resource_key)
                DO UPDATE SET entry_json=excluded.entry_json''',
                (request_id, flow_key, resource_key, json.dumps(entry_json, ensure_ascii=False)))
        return {'enabled': True, 'ok': True}
    except (OSError, sqlite3.Error):
        return {'enabled': True, 'ok': False}


def latest_attachment_file(request_id: str, flow_key: str, resource_key: str) -> dict[str, Any] | None:
    if not is_enabled() or not db_path().exists():
        return None
    try:
        with _connect() as connection:
            row = connection.execute('''SELECT entry_json FROM attachment_files
                WHERE request_id=? AND flow_key=? AND resource_key=?''',
                (request_id, flow_key, resource_key)).fetchone()
        result = json.loads(row[0]) if row else None
        return result if isinstance(result, dict) else None
    except (OSError, sqlite3.Error, ValueError):
        return None
