from __future__ import annotations

from apps.oa.runtime.paths import PROJECT_ROOT

import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import selectors
import socket
import subprocess
import sys


from apps.oa.runtime.control import RUNTIME_ROOT


DEFAULT_ROOT = RUNTIME_ROOT


def emit(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def socket_path(root):
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:24]
    return Path('/tmp') / f'oa-{identity}.sock'


@contextmanager
def round_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'approval.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def failure(error):
    code = str(error)
    return {'status': 'failed', 'error': code if re.fullmatch('[a-z][a-z0-9_]*', code) else type(error).__name__,
            **({'diagnostics_file': error.diagnostics_file} if getattr(error, 'diagnostics_file', None) else {}),
            **({'diagnostics_write_failed': True} if getattr(error, 'diagnostics_write_failed', False) else {})}


def dispatch(approval, run_id, request):
    operation = request['operation']
    if operation == 'collect':
        return approval.collect(run_id, workflow_key=request.get('workflow_key'), agent_id=request.get('agent_id'),
                                expected_task_id=request.get('expected_task_id'),
                                agent_workflow_key=request.get('agent_workflow_key'))
    if operation == 'submit':
        return approval.submit(request['task_id'], opinion=request.get('opinion'),
                               recognition=request.get('recognition'), observations=request.get('observations'),
                               confirm_write=request.get('confirm_write', False),
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
                emit({**started, 'tasks': shown, 'total': len(tasks),
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
                                    if not input_line(approval, run_id, sys.stdin.readline()):
                                        return 0
                                elif worker is not None and selected.fileobj is worker[1]:
                                    selector.unregister(worker[1])
                                    finish_collection(approval, run_id, worker)
                                    worker = None
                                elif selected.fileobj is server:
                                    client = server.accept()[0]
                                    try:
                                        with client.makefile('r', encoding='utf-8') as incoming:
                                            request = json.loads(incoming.readline())
                                        if request.get('operation') == 'collect':
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
                approval.close(run_id)


def send(root, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path(root)))
        client.sendall((json.dumps(request, ensure_ascii=False) + '\n').encode())
        with client.makefile('r', encoding='utf-8') as incoming:
            return json.loads(incoming.readline())


def main(argv=None):
    parser = argparse.ArgumentParser(description='单轮 SQLite 审批；默认预览，不写 OA。')
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest='operation', required=True)
    pending = commands.add_parser('pending', help='重新拉取待办并保持本轮运行；EOF 或 finish 结束')
    pending.add_argument('--workflow', action='append')
    pending.add_argument('--request-id', action='append', help='仅纳入重新拉取待办中匹配的申请，可重复指定')
    pending.add_argument('--limit', type=int, default=20, help='紧凑队列展示数，默认 20，0 只显示统计')
    pending.add_argument('--batch-size', type=int, help='冻结本轮可领取的全局 FIFO 排队前缀数量，独立于展示数')
    pending.add_argument('--full', action='store_true', help='返回全部待办完整数据，忽略 --limit')
    pending.add_argument('--demo', action='store_true', help='仅使用合成 OA 与本地附件，不访问网络')
    collect = commands.add_parser('collect', help='领取当前轮的一单并冻结完整材料')
    collect.add_argument('--workflow-key')
    collect.add_argument('--agent-id', required=True)
    collect.add_argument('--expected-task-id', type=int, required=True)
    collect.add_argument('--agent-workflow-key', required=True)
    submit = commands.add_parser('submit', help='保存意见，显式确认时提交并回查')
    submit.add_argument('--task-id', type=int, required=True)
    submit.add_argument('--opinion-file', type=Path)
    submit.add_argument('--recognition-file', type=Path, help='海外识别结果或带附件来源的 DeepSeek 报告；可与意见一起保存')
    submit.add_argument('--observations-file', type=Path)
    submit.add_argument('--confirm-write', choices=['YES'])
    submit.add_argument('--allow-overseas', choices=['YES'])
    check = commands.add_parser('postcheck', help='仅核对未知提交，不重发')
    check.add_argument('--task-id', type=int, required=True)
    failed = commands.add_parser('fail', help='登记当前轮活动审核的技术失败，不访问 OA')
    failed.add_argument('--task-id', type=int, required=True)
    failed.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    if args.operation == 'pending' and args.limit < 0:
        parser.error('--limit must be non-negative')
    if args.operation == 'pending' and args.batch_size is not None and args.batch_size < 0:
        parser.error('--batch-size must be non-negative')
    if args.operation == 'submit' and not (args.opinion_file or args.recognition_file):
        parser.error('submit requires --opinion-file or --recognition-file')
    root = args.root.expanduser().resolve()
    try:
        if args.operation == 'pending':
            try:
                return serve(root, demo=args.demo, workflows=args.workflow, limit=args.limit, full=args.full,
                             batch_size=args.batch_size, request_ids=args.request_id)
            except BlockingIOError:
                emit({'status': 'already_running'})
                return 0
        request = {'operation': args.operation}
        if args.operation == 'collect':
            request.update(workflow_key=args.workflow_key, agent_id=args.agent_id,
                           expected_task_id=args.expected_task_id, agent_workflow_key=args.agent_workflow_key)
        else:
            request['task_id'] = args.task_id
        if args.operation == 'submit':
            request.update(opinion=args.opinion_file.read_text() if args.opinion_file else None,
                           recognition=json.loads(args.recognition_file.read_text()) if args.recognition_file else None,
                           observations=json.loads(args.observations_file.read_text()) if args.observations_file else None,
                           confirm_write=args.confirm_write == 'YES', allow_overseas=args.allow_overseas == 'YES')
        if args.operation == 'fail':
            request['reason'] = args.reason
        result = send(root, request)
        emit(result)
        return 1 if result.get('status') == 'refused' or (result.get('status') == 'failed' and (args.operation != 'fail' or 'error' in result)) else 0
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        emit(failure(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
