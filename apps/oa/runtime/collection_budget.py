from contextlib import contextmanager
from contextvars import ContextVar
import json
import time

from apps.oa.runtime.control import write_control_bytes_atomic


_state_file = ContextVar('collection_state_file', default=None)
_current = ContextVar('collection_phase', default=None)


@contextmanager
def tracking(path):
    token = _state_file.set(path)
    try:
        yield
    finally:
        _state_file.reset(token)


def _save(state):
    path = _state_file.get()
    if path is not None:
        write_control_bytes_atomic(path, json.dumps(state).encode())


@contextmanager
def phase(name, seconds):
    previous = _current.get()
    state = {'phase': name, 'deadline': time.monotonic() + seconds}
    if previous is not None:
        state['deadline'] = min(state['deadline'], previous['deadline'])
    token = _current.set(state)
    try:
        _save(state)
        yield
    finally:
        _current.reset(token)
        _save(previous)
