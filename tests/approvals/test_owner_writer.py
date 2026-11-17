from __future__ import annotations

import json
from pathlib import Path
import signal
import socket
import subprocess
import sys

import pytest

from apps.oa.approvals.client import send, socket_path
from tests.approvals.review_fixtures import completed_review_file, review_cli
from tests.approvals.test_cli import command, PROJECT


@pytest.fixture
def delayed_writer(tmp_path, request):
    operation = getattr(request, 'param', 'submit')
    control_path = socket_path(tmp_path / 'writer-control')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.bind(str(control_path))
        control.listen()
        control.settimeout(15)
        entry = tmp_path / 'controlled_writer.py'
        entry.write_text('''import os
import socket
import sys
sys.path.insert(0, PROJECT)
from pathlib import Path
from apps.oa.approvals import cli, demo, finance_precheck
original = demo.DemoApi.submit_and_verify

def wait_action():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.connect(CONTROL)
        channel.sendall(b'writing')
        action = channel.recv(1)
    if action == b'x':
        os._exit(17)
    if action == b'u':
        return {'submit_ok': False, 'done_hit': False, 'postcheck_done_hit': False, 'failure_code': 'synthetic_unknown'}
    return action

def submit(self, request_id, remark):
    if OPERATION == 'postcheck':
        Path(MARKER).touch()
        return {'submit_ok': False, 'done_hit': False, 'postcheck_done_hit': False, 'failure_code': 'synthetic_unknown'}
    action = wait_action()
    if isinstance(action, dict):
        return action
    return original(self, request_id, remark)

original_predict = demo.DemoApi.predict
original_pending = demo.DemoApi.pending_rows
original_collect = demo.collect_demo

def predict(self, request_id, **options):
    wait_action()
    return original_predict(self, request_id, **options)

def pending(self, **options):
    if Path(MARKER).is_file():
        wait_action()
    return original_pending(self, **options)

def collect(request_id, **options):
    if Path(BLOCK_COLLECTION).is_file() and request_id == '90003':
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.connect(CONTROL)
            channel.sendall(b'collecting')
            channel.recv(1)
    return original_collect(request_id, **options)

if OPERATION == 'preview':
    finance_precheck.load_policy = lambda: {'synthetic': True}
    demo.DemoApi.predict = predict
if OPERATION == 'postcheck':
    demo.DemoApi.pending_rows = pending
demo.collect_demo = collect
demo.DemoApi.submit_and_verify = submit
if __name__ == '__main__':
    raise SystemExit(cli.main())
'''.replace('PROJECT', repr(str(PROJECT))).replace('CONTROL', repr(str(control_path))).replace('OPERATION', repr(operation)).replace('MARKER', repr(str(tmp_path / 'postcheck-delay'))).replace('BLOCK_COLLECTION', repr(str(tmp_path / 'block-collection'))))
        process = subprocess.Popen([sys.executable, str(entry), '--root', str(tmp_path), 'start', '--demo'],
            cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        channels = []
        try:
            started = json.loads(process.stdout.readline())
            def invoke(*arguments):
                result = subprocess.run(command(tmp_path, *arguments), cwd=PROJECT, capture_output=True,
                                        text=True, timeout=5)
                return json.loads(result.stdout)

            first = review_cli(tmp_path, invoke, '1', 'first', 'travel_expense')
            assert first['status'] == 'reviewing'
            assert deliver(tmp_path, invoke, 1)['status'] == 'reviewed'
            if operation == 'postcheck':
                assert invoke('submit', '--task-id', '1', '--confirm-write', 'YES')['status'] == 'unknown'
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(10)
            client.connect(started['socket'])
            client.sendall((json.dumps({'operation': operation, 'task_id': 1, 'confirm_write': True}) + '\n').encode())
            channels.append(client)
            channel = control.accept()[0]
            channel.settimeout(10)
            channels.append(channel)
            assert channel.recv(32) == b'writing'
            yield process, started, invoke, client, channel, control
        finally:
            for channel in channels:
                channel.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            control_path.unlink(missing_ok=True)


def deliver(root, invoke, task_id):
    task = invoke('show', '--task-id', str(task_id))['task']
    opinion = root / f'opinion-{task_id}.txt'
    opinion.write_text('Synthetic complete review')
    return invoke('deliver', '--task-id', str(task_id), '--agent-id', task['agent_id'],
        '--material-file', task['material_file'], '--material-sha256', task['material_sha256'],
        '--opinion-file', str(opinion), '--review-file', str(completed_review_file(root, task_id)))


def test_owner_services_collection_review_and_delivery_during_single_writer(delayed_writer, tmp_path):
    process, started, invoke, client, channel, control = delayed_writer
    status = invoke('status', '--view', 'batch')
    assert status['owner']['reachable'] is True
    assert status['owner']['writer']['operation'] == 'submit'
    assert status['owner']['writer']['task_id'] == 1
    assert not any(item['code'] == 'submission_unresolved' for item in status['alerts'])
    assert review_cli(tmp_path, invoke, '2', 'second', 'business_entertainment')['status'] == 'reviewing'
    assert deliver(tmp_path, invoke, 2)['status'] == 'reviewed'
    assert review_cli(tmp_path, invoke, '3', 'third', 'general_expense')['status'] == 'reviewing'
    assert invoke('preview', '--task-id', '2')['reason'] == 'writer_in_progress'
    refused = invoke('finish', '--run-id', started['run_id'], '--require-drained')
    assert refused['reason'] == 'run_not_drained'
    assert process.poll() is None
    channel.sendall(b'r')
    result = json.loads(client.makefile().readline())
    assert result['status'] == 'submitted' and result['postcheck_done_hit'] is True
    assert invoke('preview', '--task-id', '2')['status'] == 'preview'
    assert invoke('show', '--task-id', '1')['task']['status'] == 'submitted'
    assert len(invoke('show', '--task-id', '1')['submissions']) == 1
    invoke('finish', '--run-id', started['run_id'])
    process.wait(timeout=10)
    assert process.returncode == 0, process.stderr.read()


@pytest.mark.parametrize('action', [b'x', b'u'])
def test_unknown_or_crashed_writer_blocks_dispatch_and_never_resends(delayed_writer, tmp_path, action):
    process, started, invoke, client, channel, control = delayed_writer
    prepared = invoke('collect', '--expected-task-id', '2')
    assert prepared['status'] == 'prepared'
    channel.sendall(action)
    receipt = json.loads(client.makefile().readline())
    assert receipt['status'] == 'unknown'
    assert invoke('submit', '--task-id', '2')['error'] == 'task_not_reviewable'
    from apps.oa.approvals.store import Store
    from tests.approvals.review_fixtures import recognition_report

    store = Store(tmp_path / 'approval.sqlite3', read_only=True)
    try:
        task = store.get_task(2)
    finally:
        store.connection.close()
    result = send(tmp_path, {'operation': 'dispatch-review', 'expected_task_id': 2,
        'agent_id': 'second', 'agent_workflow_key': 'business_entertainment', 'recognition': recognition_report(task)})
    assert result['reason'] == 'run_blocked'
    assert invoke('postcheck', '--task-id', '1')['status'] == 'unknown'
    status = invoke('status', '--view', 'batch')
    assert status['run']['status'] == 'blocked'
    assert status['owner']['writer'] is None
    assert status['acceptance']['attempt_count'] == 1
    assert invoke('submit', '--task-id', '1')['status'] == 'unknown'
    assert len(invoke('show', '--task-id', '1')['submissions']) == 1
    invoke('finish', '--run-id', started['run_id'])
    process.wait(timeout=10)


@pytest.mark.parametrize('stop', ['finish', 'eof', 'terminate', 'user_stop'])
def test_close_drains_writer_and_preserves_saved_results(delayed_writer, tmp_path, stop):
    process, started, invoke, client, channel, control = delayed_writer
    assert review_cli(tmp_path, invoke, '2', 'second', 'business_entertainment')['status'] == 'reviewing'
    saved = deliver(tmp_path, invoke, 2)
    before = invoke('show', '--task-id', '2')
    closing = None
    if stop in {'finish', 'user_stop'}:
        closing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        closing.settimeout(10)
        closing.connect(started['socket'])
        request = {'operation': 'finish', 'run_id': started['run_id']}
        if stop == 'user_stop':
            request['stop'] = {'kind': 'user_stop', 'reason': 'Explicit synthetic stop'}
        closing.sendall((json.dumps(request) + '\n').encode())
        assert invoke('status')['owner']['phase'] == 'finishing'
        assert invoke('collect', '--expected-task-id', '3')['reason'] == 'run_finishing'
    elif stop == 'eof':
        process.stdin.close()
    else:
        process.send_signal(signal.SIGTERM)
    assert process.poll() is None
    channel.sendall(b'r')
    assert json.loads(client.makefile().readline())['status'] == 'submitted'
    if closing is not None:
        try:
            assert json.loads(closing.makefile().readline())['status'] == 'closed'
        finally:
            closing.close()
    process.wait(timeout=10)
    assert process.returncode == (130 if stop == 'terminate' else 0), process.stderr.read()
    after = invoke('show', '--task-id', '2')
    assert after['task']['status'] == 'reviewed'
    assert after['reviews'] == before['reviews']
    assert all(Path(path).is_file() for path in saved['evidence_files'])
    status = invoke('status', '--view', 'batch')
    assert status['owner']['reachable'] is False
    assert status['run']['status'] == 'closed'
    assert status['closeout']['drained'] is False
    if stop == 'user_stop':
        assert status['execution']['stop']['kind'] == 'user_stop'


def test_reachable_owner_blocks_an_orphan_started_attempt_before_new_dispatch(tmp_path):
    from apps.oa.approvals.store import Store
    from tests.approvals.test_capability_cli import record_submission

    process = subprocess.Popen(command(tmp_path, 'start', '--demo'), cwd=PROJECT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        started = json.loads(process.stdout.readline())
        store = Store(tmp_path / 'approval.sqlite3')
        try:
            record_submission(store, 1)
        finally:
            store.connection.close()
        result = send(tmp_path, {'operation': 'dispatch-review', 'expected_task_id': 2,
            'agent_id': 'second', 'agent_workflow_key': 'business_entertainment'})
        assert result['status'] == 'blocked'
        assert send(tmp_path, {'operation': 'status'})['status'] == 'blocked'
        from apps.oa.approvals.client import query
        shown = query(tmp_path, 'show', task_id=1)
        assert shown['task']['status'] == 'unknown'
        assert shown['submissions'][0]['status'] == 'unknown'
        assert send(tmp_path, {'operation': 'submit', 'task_id': 1, 'confirm_write': True})['status'] == 'unknown'
        assert len(query(tmp_path, 'show', task_id=1)['submissions']) == 1
        send(tmp_path, {'operation': 'finish', 'run_id': started['run_id']})
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize('delayed_writer', ['preview', 'postcheck'], indirect=True)
def test_preview_and_postcheck_leave_owner_responsive(delayed_writer, tmp_path):
    process, started, invoke, client, channel, control = delayed_writer
    status = invoke('status', '--view', 'batch')
    operation = status['owner']['writer']['operation']
    assert status['owner']['reachable'] is True
    if operation == 'preview':
        assert review_cli(tmp_path, invoke, '2', 'second', 'business_entertainment')['status'] == 'reviewing'
        assert deliver(tmp_path, invoke, 2)['status'] == 'reviewed'
    else:
        assert invoke('collect', '--expected-task-id', '2')['status'] == 'blocked'
        assert invoke('dispatch-review', '--expected-task-id', '2', '--agent-id', 'second',
                      '--agent-workflow-key', 'business_entertainment')['status'] == 'blocked'
        assert send(tmp_path, {'operation': 'deliver', 'task_id': 2})['error'] == 'run_blocked'
    assert invoke('preview', '--task-id', '2')['reason'] == 'writer_in_progress'
    channel.sendall(b'r')
    assert json.loads(client.makefile().readline())['status'] == ('preview' if operation == 'preview' else 'unknown')
    invoke('finish', '--run-id', started['run_id'])
    process.wait(timeout=10)


def test_finish_waits_for_both_actors_and_preserves_both_final_results(delayed_writer, tmp_path):
    process, started, invoke, client, channel, control = delayed_writer
    assert review_cli(tmp_path, invoke, '2', 'second', 'business_entertainment')['status'] == 'reviewing'
    (tmp_path / 'block-collection').touch()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as collector:
        collector.settimeout(10)
        collector.connect(started['socket'])
        collector.sendall(b'{"operation":"collect","expected_task_id":3}\n')
        collection_channel = control.accept()[0]
        try:
            assert collection_channel.recv(32) == b'collecting'
            status = invoke('status', '--view', 'batch')
            assert status['owner']['collection'] is not None and status['owner']['writer'] is not None
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as closer:
                closer.settimeout(10)
                closer.connect(started['socket'])
                closer.sendall((json.dumps({'operation': 'finish', 'run_id': started['run_id']}) + '\n').encode())
                assert invoke('status')['owner']['phase'] == 'finishing'
                channel.sendall(b'r')
                assert json.loads(client.makefile().readline())['status'] == 'submitted'
                assert process.poll() is None
                collection_channel.sendall(b'r')
                assert json.loads(collector.makefile().readline())['status'] == 'prepared'
                assert json.loads(closer.makefile().readline())['status'] == 'closed'
        finally:
            collection_channel.close()
    process.wait(timeout=10)
    assert invoke('show', '--task-id', '2')['task']['status'] == 'reviewing'
    assert invoke('show', '--task-id', '3')['task']['status'] == 'prepared'
    assert invoke('status', '--view', 'batch')['closeout']['drained'] is False


def test_stdin_preserves_request_order_until_socket_writer_finishes(delayed_writer, tmp_path):
    import selectors

    process, started, invoke, client, channel, control = delayed_writer
    process.stdin.write('{"operation":"status"}\n')
    process.stdin.flush()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=0.1) == []
        channel.sendall(b'r')
        assert json.loads(client.makefile().readline())['status'] == 'submitted'
        assert selector.select(timeout=5)
    result = json.loads(process.stdout.readline())
    assert result['status'] == 'running' and result['writer'] is None
    process.stdin.write('{"operation":"finish"}\n')
    process.stdin.flush()
    process.wait(timeout=10)


def test_disconnected_writer_client_keeps_definitive_receipt(delayed_writer, tmp_path):
    process, started, invoke, client, channel, control = delayed_writer
    client.close()
    channel.sendall(b'r')
    process.stdin.write('{"operation":"status"}\n')
    process.stdin.flush()
    assert json.loads(process.stdout.readline())['writer'] is None
    shown = invoke('show', '--task-id', '1')
    assert shown['task']['status'] == 'submitted'
    assert len(shown['submissions']) == 1
    full = invoke('show', '--task-id', '1', '--full')
    receipt = json.loads(full['submissions'][0]['result_json'])
    assert all(receipt[key] is True for key in ('submit_ok', 'done_hit', 'postcheck_done_hit'))
    assert invoke('submit', '--task-id', '1')['status'] == 'submitted'
    assert len(invoke('show', '--task-id', '1')['submissions']) == 1
    invoke('finish', '--run-id', started['run_id'])
    process.wait(timeout=10)


def test_exited_writer_is_reconciled_before_simultaneously_ready_dispatch(delayed_writer, tmp_path):
    import time

    process, started, invoke, client, channel, control = delayed_writer
    assert invoke('collect', '--expected-task-id', '2')['status'] == 'prepared'
    writer_pid = invoke('status')['owner']['writer']['pid']
    from apps.oa.approvals.store import Store
    from tests.approvals.review_fixtures import recognition_report

    store = Store(tmp_path / 'approval.sqlite3', read_only=True)
    try:
        task = store.get_task(2)
    finally:
        store.connection.close()
    process.send_signal(signal.SIGSTOP)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as dispatch_client:
            dispatch_client.settimeout(10)
            dispatch_client.connect(started['socket'])
            dispatch_client.sendall((json.dumps({'operation': 'dispatch-review', 'expected_task_id': 2,
                'agent_id': 'second', 'agent_workflow_key': 'business_entertainment',
                'recognition': recognition_report(task)}) + '\n').encode())
            channel.sendall(b'x')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = subprocess.run(['ps', '-p', str(writer_pid), '-o', 'stat='],
                    capture_output=True, text=True, timeout=1)
                if 'Z' in state.stdout or state.returncode != 0:
                    break
                time.sleep(0.01)
            else:
                pytest.fail('Synthetic Writer did not exit')
            process.send_signal(signal.SIGCONT)
            result = json.loads(dispatch_client.makefile().readline())
            assert result['status'] == 'blocked'
    finally:
        process.send_signal(signal.SIGCONT)
    assert json.loads(client.makefile().readline())['status'] == 'unknown'
    assert invoke('status')['run']['status'] == 'blocked'
    invoke('finish', '--run-id', started['run_id'])
    process.wait(timeout=10)
