from contextlib import contextmanager
from contextvars import ContextVar
import json
import time

from apps.oa.runtime.control import write_control_bytes_atomic


_state_file = ContextVar('collection_state_file', default=None)
_current = ContextVar('collection_phase', default=None)
_read_results = ContextVar('collection_read_results', default=None)


@contextmanager
def tracking(path, *, run_id=None, task_id=None, agent_id=None):
    token = _state_file.set(path)
    results_token = _read_results.set({'run_id': run_id, 'task_id': task_id,
                                      'agent_id': agent_id, 'read_results': []})
    try:
        yield
    finally:
        _read_results.reset(results_token)
        _state_file.reset(token)


def _save(state):
    path = _state_file.get()
    if path is not None:
        write_control_bytes_atomic(path, json.dumps(state).encode())


def record_read(result):
    path = _state_file.get()
    records = _read_results.get()
    if path is None or records is None:
        return
    records['read_results'].append(result)
    write_control_bytes_atomic(path.with_suffix('.read.json'), json.dumps(records, ensure_ascii=False).encode())


@contextmanager
def phase(name, seconds):
    previous = _current.get()
    state = {'phase': name, 'deadline': time.monotonic() + seconds,
             'started_at': time.time()}
    records = _read_results.get()
    if records is not None:
        state.update({key: records[key] for key in ('run_id', 'task_id', 'agent_id')})
    if previous is not None:
        state['deadline'] = min(state['deadline'], previous['deadline'])
    token = _current.set(state)
    try:
        _save(state)
        yield
    finally:
        _current.reset(token)
        _save(previous)
