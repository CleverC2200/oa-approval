from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import fcntl
import json
import multiprocessing
import os
import selectors
import socket
import subprocess
import sys

from apps.oa.runtime.paths import PROJECT_ROOT
from apps.oa.approvals.client import emit, failure, socket_path


@contextmanager
def round_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'approval.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield

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

def input_line(approval, run_id, line):
    if not line:
        return False
    try:
        request = json.loads(line)
        if request.get('operation') == 'finish':
            return False
        emit(dispatch(approval, run_id, request))
    except Exception as error:
        emit(failure(error))
    return True

def collect_process(root, demo, run_id, request, output):
    from apps.oa.approvals.operations import Approval

    approval = None
    try:
        if demo:
            from apps.oa.approvals.demo import create_demo
            approval = create_demo(root)
        else:
            approval = Approval(root)
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

def finish_collection(approval, run_id, worker):
    process, incoming, client = worker
    try:
        result = incoming.recv()
    except EOFError:
        for task in approval.store.tasks(run_id):
            if task['status'] == 'preparing':
                approval.store.fail_collection(task['id'], 'collection_process_failed')
        result = {'status': 'failed', 'error': 'collection_process_failed'}
    finally:
        process.join()
        incoming.close()
        process.close()
    reply(client, result)

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

def serve(root, *, demo=False, workflows=None, limit=20, full=False, batch_size=None, request_ids=None):
    from apps.oa.approvals.operations import Approval

    approval = None
    worker = None
    finishing = False
    finish_clients = []
    server_path = socket_path(root)
    with round_lock(root):
        if not demo:
            require_production_main()
        if demo:
            from apps.oa.approvals.demo import create_demo
            approval = create_demo(root)
        else:
            approval = Approval(root)
        started = approval.pending(workflows=workflows, batch_size=batch_size, request_ids=request_ids)
        run_id = started['run_id']
        server_path.unlink(missing_ok=True)
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
                    for line in sys.stdin:
                        if not input_line(approval, run_id, line):
                            break
                    return 0
                with selectors.DefaultSelector() as selector:
                    selector.register(server, selectors.EVENT_READ)
                    selector.register(sys.stdin, selectors.EVENT_READ)
                    try:
                        while True:
                            for selected, _events in selector.select():
                                if selected.fileobj is sys.stdin:
                                    if worker is not None:
                                        selector.unregister(worker[1])
                                        finish_collection(approval, run_id, worker)
                                        worker = None
                                    if finishing:
                                        return 0
                                    if not input_line(approval, run_id, sys.stdin.readline()):
                                        return 0
                                elif worker is not None and selected.fileobj is worker[1]:
                                    selector.unregister(worker[1])
                                    finish_collection(approval, run_id, worker)
                                    worker = None
                                    if finishing:
                                        return 0
                                elif selected.fileobj is server:
                                    client = server.accept()[0]
                                    try:
                                        with client.makefile('r', encoding='utf-8') as incoming:
                                            request = json.loads(incoming.readline())
                                        if request.get('operation') == 'finish':
                                            if request.get('run_id') != run_id:
                                                raise ValueError('run_mismatch')
                                            finishing = True
                                            finish_clients.append(client)
                                            if worker is None:
                                                return 0
                                            continue
                                        if request.get('operation') == 'collect':
                                            if finishing:
                                                reply(client, {'run_id': run_id, 'status': 'blocked', 'reason': 'run_finishing'})
                                                continue
                                            if worker is not None:
                                                blocked = approval.store.run(run_id)['status'] != 'running'
                                                result = {'run_id': run_id, 'status': 'blocked' if blocked else 'waiting',
                                                          'reason': 'run_blocked' if blocked else 'collection_in_progress'}
                                            else:
                                                context = multiprocessing.get_context('spawn')
                                                receiver, output = context.Pipe(duplex=False)
                                                process = context.Process(target=collect_process,
                                                                          args=(root, demo, run_id, request, output))
                                                try:
                                                    process.start()
                                                except Exception:
                                                    receiver.close()
                                                    output.close()
                                                    process.close()
                                                    raise
                                                output.close()
                                                worker = process, receiver, client
                                                selector.register(receiver, selectors.EVENT_READ)
                                                continue
                                        else:
                                            result = dispatch(approval, run_id, request)
                                    except Exception as error:
                                        result = failure(error)
                                    reply(client, result)
                    finally:
                        if worker is not None:
                            finish_collection(approval, run_id, worker)
                            worker = None
        finally:
            server_path.unlink(missing_ok=True)
            if approval is not None:
                closed = approval.close(run_id)
                for client in finish_clients:
                    reply(client, closed)
