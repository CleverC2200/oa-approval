from __future__ import annotations

from collections import Counter
from contextlib import ExitStack, contextmanager
import fcntl
import json
import multiprocessing
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading

from apps.oa.runtime.paths import PROJECT_ROOT
from apps.oa.approvals.client import emit, failure, socket_path
from apps.oa.approvals.source_policy import load_source_policy


_STDIN_EOF = object()


@contextmanager
def termination_cleanup():
    stopping = threading.Event()

    def interrupt(signum, frame):
        stopping.set()

    previous = {signum: signal.signal(signum, interrupt) for signum in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield stopping
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def round_lock(root, *, shared_root=None):
    with ExitStack() as locks:
        for directory in dict.fromkeys(path.resolve() for path in (shared_root, root) if path is not None):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = locks.enter_context((directory / 'approval.lock').open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def finish_orphaned(root, run_id):
    from apps.oa.approvals.status import submission_acceptance
    from apps.oa.approvals.store import Store

    database = root / 'approval.sqlite3'
    if not database.is_file():
        raise ValueError('run_not_found')
    with round_lock(root, shared_root=PROJECT_ROOT / 'runtime'):
        server_path = socket_path(root)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            try:
                client.connect(str(server_path))
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            else:
                raise ValueError('owner_still_reachable')
        store = Store(database)
        try:
            latest = store.latest_run()
            if latest is None or latest['id'] != run_id:
                raise ValueError('run_mismatch')
            if latest['status'] not in {'running', 'blocked'}:
                raise ValueError('run_not_active')
            tasks = store.tasks(run_id)
            if any(task['status'] == 'preparing' for task in tasks):
                raise ValueError('collection_unresolved')
            acceptance = submission_acceptance(store, run_id, tasks)
            if any(task['status'] in {'submitting', 'unknown'} for task in tasks) or any(
                    acceptance[key] for key in ('unknown_attempt_count', 'unfinished_attempt_count', 'unverified_attempt_count')):
                raise ValueError('submission_unresolved')
            result = store.close(run_id)
            server_path.unlink(missing_ok=True)
            return {**result, 'reason': 'owner_missing'}
        finally:
            store.connection.close()


def dispatch(approval, run_id, request):
    operation = request['operation']
    if operation == 'collect':
        return approval.collect(run_id, workflow_key=request.get('workflow_key'), agent_id=request.get('agent_id'),
                                expected_task_id=request.get('expected_task_id'),
                                agent_workflow_key=request.get('agent_workflow_key'))
    if operation == 'status':
        return {'run_id': run_id, 'status': approval.store.run(run_id)['status']}
    if operation in {'submit', 'preview'}:
        return approval.submit(request['task_id'], opinion=request.get('opinion'),
                               recognition=request.get('recognition'), observations=request.get('observations'),
                               confirm_write=operation == 'submit' and request.get('confirm_write', False),
                               allow_overseas=request.get('allow_overseas', False))
    if operation == 'postcheck':
        return approval.postcheck(request['task_id'])
    if operation == 'fail':
        return approval.fail(run_id, request['task_id'], request['reason'])
    raise ValueError('unknown_operation')

def collect_process(root, demo, run_id, request, output, source_policy):
    from apps.oa.approvals.operations import Approval

    approval = None
    try:
        if demo:
            from apps.oa.approvals.demo import create_demo
            approval = create_demo(root)
        else:
            approval = Approval(root, source_policy=source_policy)
        result = dispatch(approval, run_id, request)
    except Exception as error:
        result = failure(error)
    finally:
        if approval is not None:
            approval.store.connection.close()
    try:
        output.send(result)
    finally:
        output.close()

def reply(client, result):
    try:
        client.sendall((json.dumps(result, ensure_ascii=False, default=str) + '\n').encode())
    except OSError:
        pass
    finally:
        client.close()

class _Collection:
    def __init__(self, process, incoming, respond, store, run_id):
        self.process = process
        self.incoming = incoming
        self._respond = respond
        self._store = store
        self._run_id = run_id

    @classmethod
    def start(cls, root, demo, run_id, request, respond, store, source_policy):
        context = multiprocessing.get_context('spawn')
        receiver, output = context.Pipe(duplex=False)
        process = context.Process(target=collect_process, args=(root, demo, run_id, request, output, source_policy))
        try:
            process.start()
        except Exception:
            receiver.close()
            output.close()
            process.close()
            raise
        output.close()
        return cls(process, receiver, respond, store, run_id)

    def complete(self):
        try:
            result = self.incoming.recv()
        except EOFError:
            for task in self._store.tasks(self._run_id):
                if task['status'] == 'preparing':
                    self._store.fail_collection(task['id'], 'collection_process_failed')
            result = {'status': 'failed', 'error': 'collection_process_failed'}
        finally:
            self.process.join()
            self.incoming.close()
            self.process.close()
        self._respond(result)


class _RoundSession:
    def __init__(self, approval, run_id, *, root, demo, close_transport):
        self._approval = approval
        self._run_id = run_id
        self._root = root
        self._demo = demo
        self._close_transport = close_transport
        self._collection = None
        self._finishing = False
        self._finish_responders = []
        self._closed_result = None
        self._close_attempted = False
        self._close_error = None
        self._handoff = False

    @property
    def collection_pipe(self):
        return self._collection.incoming if self._collection is not None else None

    @property
    def closed(self):
        return self._closed_result is not None

    def handle(self, receive_request, *, source, respond):
        if source == 'stdin':
            self.collection_ready()
        if self.closed:
            return
        try:
            request = receive_request()
            if source == 'stdin' and (request is _STDIN_EOF or request.get('operation') == 'finish'):
                self._handoff = request is not _STDIN_EOF and request.get('handoff', False)
                self._finishing = True
            elif request.get('operation') == 'finish':
                if request.get('run_id') != self._run_id:
                    raise ValueError('run_mismatch')
                self._finishing = True
                self._handoff = request.get('handoff', False)
                self._finish_responders.append(respond)
            elif source == 'socket' and request.get('operation') == 'collect':
                self._collect(request, respond)
            else:
                respond(dispatch(self._approval, self._run_id, request))
        except Exception as error:
            respond(failure(error))
        self._close_if_drained()

    def _collect(self, request, respond):
        if self._finishing:
            respond({'run_id': self._run_id, 'status': 'blocked', 'reason': 'run_finishing'})
        elif self._collection is not None:
            blocked = self._approval.store.run(self._run_id)['status'] != 'running'
            respond({'run_id': self._run_id, 'status': 'blocked' if blocked else 'waiting',
                     'reason': 'run_blocked' if blocked else 'collection_in_progress'})
        else:
            self._collection = _Collection.start(self._root, self._demo, self._run_id, request,
                                                 respond, self._approval.store, self._approval.source_policy)

    def _drain_collection(self):
        if self._collection is not None:
            collection, self._collection = self._collection, None
            collection.complete()

    def _close_if_drained(self):
        if self._finishing and self._collection is None:
            self.shutdown()

    def collection_ready(self):
        self._drain_collection()
        self._close_if_drained()

    def shutdown(self):
        if self._close_attempted:
            if self._close_error is not None:
                raise self._close_error
            return self._closed_result
        self._finishing = True
        try:
            self._drain_collection()
        finally:
            self._close_attempted = True
            try:
                self._close_transport()
                self._closed_result = self._approval.close(self._run_id, handoff=self._handoff)
            except Exception as error:
                self._close_error = error
                raise
            responders, self._finish_responders = self._finish_responders, []
            for respond in responders:
                respond(self._closed_result)
        return self._closed_result


def _read_stdin():
    line = sys.stdin.readline()
    return json.loads(line) if line else _STDIN_EOF


def _read_socket(client):
    with client.makefile('r', encoding='utf-8') as incoming:
        return json.loads(incoming.readline())


def require_production_main(project=None):
    project = project or PROJECT_ROOT
    environment = dict(os.environ)
    for name in subprocess.check_output(['git', 'rev-parse', '--local-env-vars'], text=True).splitlines():
        environment.pop(name, None)
    environment['GIT_TERMINAL_PROMPT'] = '0'
    def git(*args):
        try:
            result = subprocess.run(['git', '-C', str(project), *args], text=True, capture_output=True, env=environment, timeout=30)
        except subprocess.TimeoutExpired:
            raise ValueError('production_main_unpublished') from None
        if result.returncode:
            raise ValueError('production_main_unpublished')
        return result.stdout.strip()
    if git('branch', '--show-current') != 'main':
        raise ValueError('production_main_required')
    if git('status', '--porcelain=v1'):
        raise ValueError('production_checkout_dirty')
    git('fetch', '--quiet', 'origin', 'refs/heads/main:refs/remotes/origin/main')
    if git('rev-parse', 'HEAD') != git('rev-parse', 'origin/main'):
        raise ValueError('production_main_not_synced')

def serve(root, *, demo=False, workflows=None, limit=20, full=False, batch_size=None, request_ids=None,
          continue_from=None):
    from apps.oa.approvals.operations import Approval

    server_path = socket_path(root)
    with termination_cleanup() as stopping, round_lock(root, shared_root=None if demo else PROJECT_ROOT / 'runtime'):
        source_policy = None
        if not demo:
            require_production_main()
            source_policy = load_source_policy()
        if demo:
            from apps.oa.approvals.demo import create_demo
            approval = create_demo(root)
        else:
            approval = Approval(root, source_policy=source_policy)
        started = approval.pending(workflows=workflows, batch_size=batch_size, request_ids=request_ids,
                                   continue_from=continue_from)
        run_id = started['run_id']
        server_path.unlink(missing_ok=True)
        server = None

        def close_transport():
            if server is not None:
                server.close()
            server_path.unlink(missing_ok=True)

        session = _RoundSession(approval, run_id, root=root, demo=demo, close_transport=close_transport)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(server_path))
                server_path.chmod(0o600)
                server.listen()
                tasks = started['tasks']
                if full:
                    shown = tasks
                else:
                    titles = {request['request_id']: request['title'] for request in approval.store.requests()}
                    shown = [
                        {**{key: task[key] for key in ('id', 'request_id', 'received_at', 'workflow_key', 'status')},
                         'title': titles[task['request_id']]}
                        for task in tasks[:limit]
                    ]
                emit({**started, 'status': 'ready', 'tasks': shown, 'total': len(tasks),
                      'by_workflow': dict(Counter(task['workflow_key'] for task in tasks)),
                      'shown': len(shown), 'truncated': len(shown) < len(tasks),
                      'demo': demo, 'socket': str(server_path)})
                if sys.stdin.seekable():
                    while not session.closed:
                        if stopping.is_set():
                            return 130
                        session.handle(_read_stdin, source='stdin', respond=emit)
                    return 0
                with selectors.DefaultSelector() as selector:
                    selector.register(server, selectors.EVENT_READ, 'socket')
                    selector.register(sys.stdin, selectors.EVENT_READ, 'stdin')
                    watched_pipe = None
                    while not session.closed:
                        if stopping.is_set():
                            return 130
                        for selected, _events in selector.select(timeout=0.5):
                            if stopping.is_set():
                                return 130
                            if selected.data == 'stdin':
                                session.handle(_read_stdin, source='stdin', respond=emit)
                            elif selected.data == 'collection':
                                if selected.fd != watched_pipe:
                                    continue
                                selector.unregister(watched_pipe)
                                watched_pipe = None
                                session.collection_ready()
                            else:
                                client = server.accept()[0]
                                session.handle(lambda: _read_socket(client), source='socket',
                                               respond=lambda result, client=client: reply(client, result))
                            if session.closed:
                                return 0
                            incoming = session.collection_pipe
                            current_pipe = incoming.fileno() if incoming is not None else None
                            if current_pipe != watched_pipe:
                                if watched_pipe is not None:
                                    selector.unregister(watched_pipe)
                                if current_pipe is not None:
                                    selector.register(current_pipe, selectors.EVENT_READ, 'collection')
                                watched_pipe = current_pipe
        finally:
            session.shutdown()
