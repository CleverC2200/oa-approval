from __future__ import annotations

import os
from pathlib import Path
import stat
from threading import Event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = PROJECT_ROOT / 'runtime'
STOP_CODE = 'oa_run_stopped'
_stopped = Event()


def stop_requested() -> bool:
    return _stopped.is_set() or os.environ.get('OA_STOP_REQUESTED', '').lower() in {'1', 'true', 'yes'}


def request_stop(reason: str | None = None) -> None:
    _stopped.set()


def stop_error(message: str) -> dict:
    return {'code': STOP_CODE, 'message': message}


def read_control_bytes(path: Path, *, max_bytes: int = 1_048_576) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('attachment_source_not_regular')
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError('attachment_source_too_large')
    return raw


def write_control_bytes_atomic(path: Path, payload: bytes) -> None:
    from .material_store import write_bytes

    write_bytes(path, payload)
