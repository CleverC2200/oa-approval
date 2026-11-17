from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path

from apps.oa.runtime.control import read_control_bytes, write_control_bytes_atomic


def save_progress(path, page):
    path = Path(path).absolute()
    if path.resolve() == Path(page['material_file']).resolve() or path.is_symlink():
        raise ValueError('material_progress_path_invalid')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = read_control_bytes(path)
        saved = json.loads(raw) if raw is not None else {'kind': 'material_read_progress', 'documents': []}
        if not isinstance(saved, dict) or saved.get('kind') != 'material_read_progress' or not isinstance(saved.get('documents'), list):
            raise ValueError('material_progress_file_invalid')
        if any(not isinstance(item, dict) or not isinstance(item.get('ranges'), list) for item in saved['documents']):
            raise ValueError('material_progress_file_invalid')
        identity = {key: page[key] for key in ('material_file', 'material_sha256', 'source_pointer', 'view', 'total_chars')}
        document = next((item for item in saved['documents'] if all(item.get(key) == value for key, value in identity.items())), None)
        if document is None:
            document = identity | {'ranges': []}
            saved['documents'].append(document)
        ranges = sorted([*document['ranges'], [page['offset'], page['end']]])
        merged = []
        for start, end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        document['ranges'] = merged
        first_end = merged[0][1] if merged and merged[0][0] == 0 else 0
        document['next_offset'] = first_end if first_end < page['total_chars'] else None
        saved.update(coverage='supplied_ranges_only', updated_at=datetime.now(timezone.utc).isoformat())
        write_control_bytes_atomic(path, json.dumps(saved, ensure_ascii=False).encode())


def progress_summary(material_file, material_sha256):
    path = Path(material_file).parent / 'read-progress.json'
    try:
        raw = read_control_bytes(path)
        if raw is None:
            return {'status': 'not_recorded', 'progress_file': str(path)}
        saved = json.loads(raw)
        if saved.get('kind') != 'material_read_progress' or saved.get('coverage') != 'supplied_ranges_only':
            raise ValueError('material_progress_file_invalid')
        documents = saved['documents']
        matches = [item for item in documents if item['material_file'] == str(material_file)
                   and item['material_sha256'] == material_sha256]
        return {'status': 'recorded' if matches else 'material_version_mismatch', 'progress_file': str(path),
                'updated_at': saved['updated_at'], 'coverage': saved['coverage'],
                'documents': len(documents), 'material_documents': len(matches)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'status': 'invalid', 'progress_file': str(path)}
