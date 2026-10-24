from __future__ import annotations

import json
import io
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


PROJECT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pending_output(tmp_path, monkeypatch, capsys):
    from apps.oa.approvals import cli, owner
    from apps.oa.approvals.demo import DemoApi
    from apps.oa.approvals.sources import oa as api

    fake = DemoApi()
    fake.rows = [
        {'request_id': str(101 + index), 'received_at': '2026-10-22 09:00:00',
         'operator_id': 'tester', 'workflow_key': 'travel_expense' if index < 21 else 'general_expense',
         'request_name': f'测试单据 {index + 1}'}
        for index in reversed(range(23))
    ]
    monkeypatch.setattr(api, 'build_api', lambda _config: fake)
    monkeypatch.setattr(owner, 'require_production_main', lambda: None)
    monkeypatch.setattr(owner.socket.socket, 'bind', lambda _socket, address: Path(address).touch())
    monkeypatch.setattr(owner.socket.socket, 'listen', lambda _socket: None)

    def invoke(*arguments):
        monkeypatch.setattr(sys, 'stdin', io.StringIO('{"operation":"finish"}\n'))
        assert cli.main(['--root', str(tmp_path), 'pending', *arguments]) == 0
        return json.loads(capsys.readouterr().out)

    return invoke


def test_pending_summarizes_first_twenty_without_trimming_the_queue(tmp_path, pending_output):
    from apps.oa.approvals.operations import Approval

    started = pending_output()
    assert started['run_id']
    assert started['total'] == 23
    assert started['by_workflow'] == {'travel_expense': 21, 'general_expense': 2}
    assert started['shown'] == 20
    assert started['truncated'] is True
    assert [task['request_id'] for task in started['tasks']] == [str(value) for value in range(101, 121)]
    assert started['tasks'][0] == {
        'id': 23, 'request_id': '101', 'received_at': '2026-10-22 09:00:00',
        'workflow_key': 'travel_expense', 'status': 'queued', 'title': '测试单据 1',
    }
    assert all(set(task) == {'id', 'request_id', 'received_at', 'workflow_key', 'status', 'title'}
               for task in started['tasks'])
    approval = Approval(tmp_path)
    assert approval.inspect(1)['task']['request_id'] == '123'


@pytest.mark.parametrize('limit, shown, truncated', [(0, 0, True), (1, 1, True), (50, 23, False)])
def test_pending_limit_changes_only_display_size(pending_output, limit, shown, truncated):
    started = pending_output('--limit', str(limit))
    assert started['total'] == 23
    assert started['by_workflow'] == {'travel_expense': 21, 'general_expense': 2}
    assert started['shown'] == shown
    assert len(started['tasks']) == shown
    assert started['truncated'] is truncated


@pytest.mark.parametrize('arguments', [('--full',), ('--full', '--limit', '1')])
def test_pending_full_returns_every_complete_task(pending_output, arguments):
    started = pending_output(*arguments)
    assert started['total'] == started['shown'] == 23
    assert started['truncated'] is False
    assert len(started['tasks']) == 23
    assert started['by_workflow'] == {'travel_expense': 21, 'general_expense': 2}
    assert started['tasks'][-1]['request_id'] == '123'
    assert started['tasks'][0]['frozen_json'] is None
    assert started['tasks'][0]['material_json'] is None
    assert started['tasks'][0]['occurrence']


def test_pending_rejects_negative_limit_before_starting_a_round(tmp_path, capsys, pending_output, monkeypatch):
    from apps.oa.approvals import cli, owner

    root = tmp_path / 'not-started'
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"operation":"finish"}\n'))
    with pytest.raises(SystemExit) as error:
        cli.main(['--root', str(root), 'pending', '--limit', '-1'])
    assert error.value.code == 2
    assert '--limit must be non-negative' in capsys.readouterr().err
    assert not root.exists()


def test_pending_empty_queue_reports_zero_without_truncation(pending_output):
    started = pending_output('--workflow', 'overseas_invoice')
    assert started['total'] == started['shown'] == 0
    assert started['by_workflow'] == {}
    assert started['tasks'] == []
    assert started['truncated'] is False


def test_pending_request_filter_freezes_only_fresh_matching_requests(pending_output):
    started = pending_output('--request-id', '103', '--request-id', '101', '--request-id', '999')
    assert started['total'] == 2
    assert [task['request_id'] for task in started['tasks']] == ['101', '103']


@pytest.mark.parametrize('operation', ['submit', 'preview'])
def test_pending_summary_keeps_stdin_collect_and_default_preview(tmp_path, pending_output, monkeypatch, capsys, operation):
    from apps.oa.approvals import cli, owner

    monkeypatch.setattr(sys, 'stdin', io.StringIO(
        '{"operation":"collect","agent_id":"travel-reviewer","expected_task_id":1,"agent_workflow_key":"travel_expense"}\n'
        f'{{"operation":"{operation}","task_id":1,"opinion":"合成离线演示意见。"}}\n'
        '{"operation":"finish"}\n'))
    assert cli.main(['--root', str(tmp_path), 'pending', '--demo']) == 0
    started, collected, preview = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert started['total'] == started['shown'] == 3
    assert started['truncated'] is False
    assert collected['request_id'] == '90001'
    assert preview['status'] == 'preview'


def command(root, *arguments):
    return [sys.executable, '-m', 'apps.oa.approvals.cli', '--root', str(root), *arguments]


@pytest.mark.parametrize('arguments', [(), ('--agent-id', 'reviewer'),
    ('--agent-id', 'reviewer', '--expected-task-id', '1'),
    ('--expected-task-id', '1', '--agent-workflow-key', 'travel_expense')])
def test_collect_cli_requires_all_guards_before_connecting(tmp_path, arguments):
    result = subprocess.run(command(tmp_path, 'collect', *arguments), cwd=PROJECT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert 'required' in result.stderr
    assert not (tmp_path / 'approval.sqlite3').exists()


@pytest.mark.parametrize('transport', ['stdin', 'socket'])
@pytest.mark.parametrize('options, reason', [
    ({}, 'claim_guards_required'),
    ({'agent_id': 'reviewer'}, 'claim_guards_required'),
    ({'expected_task_id': 1, 'agent_workflow_key': 'travel_expense'}, 'agent_identity_required'),
    ({'expected_task_id': True, 'agent_workflow_key': 'travel_expense', 'agent_id': 'reviewer'}, 'claim_guards_required'),
])
def test_collect_json_transports_refuse_missing_guards_without_consuming_task(tmp_path, transport, options, reason):
    from apps.oa.approvals.cli import send

    process = subprocess.Popen(command(tmp_path, 'pending', '--demo'), cwd=PROJECT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        started = json.loads(process.stdout.readline())

        def invoke(request):
            if transport == 'socket':
                return send(tmp_path, request)
            process.stdin.write(json.dumps(request) + '\n')
            process.stdin.flush()
            return json.loads(process.stdout.readline())

        assert invoke({'operation': 'collect', **options}) == {
            'run_id': started['run_id'], 'status': 'refused', 'reason': reason, 'summary': {'queued': 3}}
        queued = started['tasks'][0]
        collected = invoke({'operation': 'collect', 'agent_id': 'reviewer',
                            'expected_task_id': queued['id'], 'agent_workflow_key': queued['workflow_key']})
        assert collected['status'] == 'reviewing'
        assert collected['task_id'] == queued['id']
        process.stdin.write('{"operation":"finish"}\n')
        process.stdin.flush()
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_demo_pending_is_exclusive_and_can_close(tmp_path):
    process = subprocess.Popen(command(tmp_path, 'pending', '--demo'), cwd=PROJECT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        started = json.loads(process.stdout.readline())
        assert started['run_id']
        assert started['demo'] is True
        assert len(started['tasks']) == 3
        duplicate = subprocess.run(command(tmp_path, 'pending', '--demo'), cwd=PROJECT,
                                   capture_output=True, text=True, timeout=10)
        assert json.loads(duplicate.stdout)['status'] == 'already_running'
        process.stdin.close()
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_demo_runs_collect_preview_submit_and_postcheck(tmp_path, monkeypatch):
    process = subprocess.Popen(command(tmp_path, 'pending', '--demo'), cwd=PROJECT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        started = json.loads(process.stdout.readline())

        def invoke(*arguments):
            result = subprocess.run(command(tmp_path, *arguments), cwd=PROJECT,
                                    capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        task = invoke('collect', '--agent-id', 'travel-reviewer',
                      '--expected-task-id', str(started['tasks'][0]['id']),
                      '--agent-workflow-key', started['tasks'][0]['workflow_key'])
        assert task['workflow_key'] == 'travel_expense'
        material = json.loads(Path(task['material_file']).read_text())
        assert len(material['prepared']['images']) == 1
        assert material['resources'][0]['name'] == 'demo.pdf'
        opinion = tmp_path / 'opinion.txt'
        opinion.write_text('合成离线演示意见，不是实际审批判断。')
        from apps.oa.materials import recognize as attachment_recognition
        monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-api-key')
        monkeypatch.setattr(attachment_recognition, '_post', lambda *_args, **_kwargs: {
            'choices': [{'finish_reason': 'stop', 'message': {'content': '合成测试金额 12.50'}}]})
        report = attachment_recognition.recognize(material_file=Path(task['material_file']),
                                                  output_dir=tmp_path / 'recognized')
        assert report['status'] == 'complete'
        preview = invoke('submit', '--task-id', str(task['task_id']), '--opinion-file', str(opinion),
                         '--recognition-file', report['report_file'])
        assert preview['status'] == 'preview'
        with sqlite3.connect(tmp_path / 'approval.sqlite3') as database:
            contents = database.execute('''SELECT a.resource_id,c.locator,c.content,c.source FROM attachment_contents c
                JOIN attachments a ON a.id=c.attachment_id
                WHERE c.locator LIKE 'material:prepared.%' ORDER BY c.id''').fetchall()
            assert contents == [(part['resource_id'], part['evidence_ref'], part['answer'], part['source'])
                                for part in report['parts']]
            assert {row[3] for row in contents} == {'deepseek', 'parser'}
            assert {row[0] for row in contents} == {'demo-pdf'}
            assert database.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0
        submitted = invoke('submit', '--task-id', str(task['task_id']), '--opinion-file', str(opinion),
                           '--confirm-write', 'YES')
        assert submitted['status'] == 'submitted'
        assert invoke('postcheck', '--task-id', str(task['task_id']))['status'] == 'submitted'
        for queued in started['tasks'][1:]:
            workflow = queued['workflow_key']
            following = invoke('collect', '--agent-id', workflow + '-reviewer',
                               '--expected-task-id', str(queued['id']), '--agent-workflow-key', workflow)
            assert following['workflow_key'] == workflow
            assert invoke('submit', '--task-id', str(following['task_id']), '--opinion-file', str(opinion),
                          '--confirm-write', 'YES')['status'] == 'submitted'
        assert invoke('collect', '--agent-id', 'third', '--expected-task-id', '3', '--agent-workflow-key', 'general_expense')['status'] == 'done'
        process.stdin.write('{"operation":"finish"}\n')
        process.stdin.flush()
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_pending_accepts_finish_from_a_regular_input_file(tmp_path):
    source = tmp_path / 'commands.jsonl'
    source.write_text('{"operation":"finish"}\n')
    with source.open() as incoming:
        result = subprocess.run(command(tmp_path, 'pending', '--demo'), cwd=PROJECT,
                                stdin=incoming, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['demo'] is True


def test_fail_command_records_review_failure_and_rejects_preview_without_overwrite(tmp_path):
    process = subprocess.Popen(command(tmp_path, 'pending', '--demo'), cwd=PROJECT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        started = json.loads(process.stdout.readline())

        def invoke(*arguments):
            result = subprocess.run(command(tmp_path, *arguments), cwd=PROJECT,
                                    capture_output=True, text=True, timeout=10)
            return result.returncode, json.loads(result.stdout)

        _, task = invoke('collect', '--agent-id', 'native-reviewer',
                         '--expected-task-id', str(started['tasks'][0]['id']),
                         '--agent-workflow-key', started['tasks'][0]['workflow_key'])
        code, failed = invoke('fail', '--task-id', str(task['task_id']), '--reason', 'native_agent_failed')
        assert code == 0
        assert failed == {'run_id':started['run_id'], 'task_id':task['task_id'], 'status':'failed',
                          'reason':'native_agent_failed', 'failure_count':1}
        assert invoke('fail', '--task-id', str(task['task_id']), '--reason', 'repeat') == (
            1, {'status':'failed', 'error':'task_not_failable'})
        _, following = invoke('collect', '--agent-id', 'native-next-reviewer',
                              '--expected-task-id', str(started['tasks'][1]['id']),
                              '--agent-workflow-key', started['tasks'][1]['workflow_key'])
        opinion = tmp_path / 'opinion.txt'
        opinion.write_text('合成离线意见。')
        assert invoke('submit', '--task-id', str(following['task_id']), '--opinion-file', str(opinion))[1]['status'] == 'preview'
        assert invoke('fail', '--task-id', str(following['task_id']), '--reason', 'too_late') == (
            1, {'status':'failed', 'error':'task_not_failable'})
        process.stdin.write('{"operation":"finish"}\n')
        process.stdin.flush()
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_socket_collect_guards_batch_independently_of_pending_display_limit(tmp_path):
    process = subprocess.Popen(command(tmp_path, 'pending', '--demo', '--batch-size', '2', '--limit', '0'), cwd=PROJECT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        started = json.loads(process.stdout.readline())
        assert started['total'] == 3
        assert started['shown'] == 0
        assert started['batch'] == {'size':2, 'task_ids':[1,2]}

        def invoke(*arguments):
            result = subprocess.run(command(tmp_path, *arguments), cwd=PROJECT,
                                    capture_output=True, text=True, timeout=10)
            return result.returncode, json.loads(result.stdout)

        assert invoke('collect', '--agent-id', 'actual-native-agent', '--expected-task-id', '1',
                      '--agent-workflow-key', 'general_expense') == (
            1, {'run_id':started['run_id'], 'status':'refused', 'reason':'agent_workflow_mismatch',
                'summary':{'queued':3}})
        code, first = invoke('collect', '--agent-id', 'actual-native-agent', '--expected-task-id', '1',
                             '--agent-workflow-key', 'travel_expense')
        assert code == 0
        assert first['task_id'] == 1
        assert invoke('fail', '--task-id', '1', '--reason', 'read_failed')[1]['status'] == 'failed'
        code, second = invoke('collect', '--agent-id', 'actual-next-native-agent', '--expected-task-id', '2',
                              '--agent-workflow-key', 'business_entertainment')
        assert code == 0
        assert second['task_id'] == 2
        assert invoke('fail', '--task-id', '2', '--reason', 'read_failed')[1]['status'] == 'failed'
        assert invoke('collect', '--agent-id', 'third', '--expected-task-id', '3', '--agent-workflow-key', 'general_expense') == (
            0, {'run_id':started['run_id'], 'status':'done', 'reason':'batch_exhausted',
                'summary':{'failed':2,'queued':1}})
        process.stdin.write('{"operation":"finish"}\n')
        process.stdin.flush()
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()
        from apps.oa.approvals.operations import Approval
        app = Approval(tmp_path)
        assert app.inspect(3)['task']['status'] == 'queued'
        assert json.loads(app.store.run(started['run_id'])['summary_json'])['batch'] == started['batch']
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.fixture
def blocked_collection(tmp_path):
    import socket

    from apps.oa.approvals.cli import socket_path
    control_path = socket_path(tmp_path / 'collector')
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control.bind(str(control_path))
    control.listen()
    control.settimeout(15)
    entry = tmp_path / 'controlled_demo.py'
    entry.write_text('''import os
from pathlib import Path
import socket
import sys
sys.path.insert(0, PROJECT)
from apps.oa.approvals import cli, demo
original_collect = demo.collect_demo

def collect(request_id, **options):
    if request_id == '90002':
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.connect(CONTROL)
            channel.sendall(b'collecting')
            action = channel.recv(1)
        if action == b'x':
            os._exit(17)
        if action == b'e':
            raise ValueError('controlled_read_failed')
    return original_collect(request_id, **options)

original_submit = demo.DemoApi.submit_and_verify

def submit(self, request_id, remark):
    if Path(UNKNOWN).exists():
        return {'submit_ok': False, 'done_hit': False, 'postcheck_done_hit': False, 'failure_code': 'controlled_unknown'}
    return original_submit(self, request_id, remark)

demo.DemoApi.submit_and_verify = submit
demo.collect_demo = collect
if __name__ == '__main__':
    raise SystemExit(cli.main())
'''.replace('PROJECT', repr(str(PROJECT))).replace('CONTROL', repr(str(control_path))).replace('UNKNOWN', repr(str(tmp_path / 'unknown'))))
    process = subprocess.Popen([sys.executable, str(entry), '--root', str(tmp_path), 'pending', '--demo'],
                               cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    clients = []
    try:
        started = json.loads(process.stdout.readline())
        def invoke(*arguments):
            result = subprocess.run(command(tmp_path, *arguments), cwd=PROJECT,
                                    capture_output=True, text=True, timeout=10)
            return json.loads(result.stdout)

        first = invoke('collect', '--agent-id', 'first', '--expected-task-id', str(started['tasks'][0]['id']),
                       '--agent-workflow-key', started['tasks'][0]['workflow_key'])
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(15)
        client.connect(started['socket'])
        client.sendall(b'{"operation":"collect","agent_id":"second","expected_task_id":2,"agent_workflow_key":"business_entertainment"}\n')
        clients.append(client)
        channel = control.accept()[0]
        channel.settimeout(15)
        clients.append(channel)
        assert channel.recv(32) == b'collecting'
        yield process, started, first, invoke, client, channel
    finally:
        for client in clients:
            client.close()
        control.close()
        control_path.unlink(missing_ok=True)
        if process.poll() is None:
            process.kill()
            process.wait()


def test_socket_submit_finishes_while_another_collection_is_blocked(blocked_collection, tmp_path):
    process, started, first, invoke, client, channel = blocked_collection
    opinion = tmp_path / 'opinion.txt'
    opinion.write_text('合成离线意见。')
    submitted = invoke('submit', '--task-id', str(first['task_id']), '--opinion-file', str(opinion),
                       '--confirm-write', 'YES')
    assert submitted['status'] == 'submitted'
    assert invoke('postcheck', '--task-id', str(first['task_id']))['status'] == 'submitted'
    assert invoke('collect', '--agent-id', 'third', '--expected-task-id', '3', '--agent-workflow-key', 'general_expense')['status'] == 'waiting'
    from apps.oa.approvals.operations import Approval
    app = Approval(tmp_path)
    assert [task['status'] for task in app.store.tasks(started['run_id'])] == ['submitted', 'preparing', 'queued']
    channel.sendall(b'r')
    assert json.loads(client.makefile().readline())['status'] == 'reviewing'
    process.stdin.close()
    process.wait(timeout=15)
    assert process.returncode == 0, process.stderr.read()


@pytest.mark.parametrize('action', [b'r', b'e'])
def test_failed_preparation_keeps_material_without_resurrecting_task(blocked_collection, tmp_path, action):
    process, started, first, invoke, client, channel = blocked_collection
    assert invoke('fail', '--task-id', '2', '--reason', 'reviewer_stopped')['failure_count'] == 1
    channel.sendall(action)
    result = json.loads(client.makefile().readline())
    assert result['status'] == 'failed'
    if action == b'r':
        assert result['reason'] == 'reviewer_stopped'
    else:
        assert result['error'] == 'controlled_read_failed'
    from apps.oa.approvals.operations import Approval
    details = Approval(tmp_path).inspect(2)
    assert details['task']['status'] == 'failed'
    assert details['task']['failure_count'] == 1
    assert details['task']['error'] == 'reviewer_stopped'
    if action == b'r':
        assert Path(details['task']['material_file']).is_file()
        assert details['attachments']
    process.stdin.close()
    process.wait(timeout=15)
    assert process.returncode == 0, process.stderr.read()


@pytest.mark.parametrize('action', [b'r', b'e', b'x'])
def test_unknown_blocks_refill_and_other_write_without_discarding_collection(blocked_collection, tmp_path, action):
    process, started, first, invoke, client, channel = blocked_collection
    (tmp_path / 'unknown').touch()
    opinion = tmp_path / 'opinion.txt'
    opinion.write_text('合成离线意见。')
    assert invoke('submit', '--task-id', '1', '--opinion-file', str(opinion),
                  '--confirm-write', 'YES')['status'] == 'unknown'
    assert invoke('collect', '--agent-id', 'third', '--expected-task-id', '3', '--agent-workflow-key', 'general_expense')['status'] == 'blocked'
    channel.sendall(action)
    result = json.loads(client.makefile().readline())
    assert result['status'] == ('reviewing' if action == b'r' else 'failed')
    from apps.oa.approvals.operations import Approval
    app = Approval(tmp_path)
    task = app.inspect(2)['task']
    assert task['status'] == ('reviewing' if action == b'r' else 'failed')
    assert task['failure_count'] == (0 if action == b'r' else 1)
    assert app.store.run(started['run_id'])['status'] == 'blocked'
    assert invoke('collect', '--agent-id', 'third', '--expected-task-id', '3', '--agent-workflow-key', 'general_expense')['status'] == 'blocked'
    assert invoke('submit', '--task-id', '2', '--opinion-file', str(opinion),
                  '--confirm-write', 'YES')['error'] in {'run_blocked', 'task_not_reviewable'}
    assert app.inspect(3)['task']['status'] == 'queued'
    assert app.inspect(2)['submissions'] == []
    process.stdin.close()
    process.wait(timeout=15)
    assert process.returncode == 0, process.stderr.read()


@pytest.mark.parametrize('stop', ['eof', 'finish'])
def test_exit_waits_for_disconnected_collection_and_keeps_lock(blocked_collection, tmp_path, stop):
    process, started, first, invoke, client, channel = blocked_collection
    client.close()
    if stop == 'finish':
        process.stdin.write('{"operation":"finish"}\n')
        process.stdin.flush()
    else:
        process.stdin.close()
    assert invoke('pending', '--demo')['status'] == 'already_running'
    channel.sendall(b'r')
    process.wait(timeout=15)
    assert process.returncode == 0, process.stderr.read()
    from apps.oa.approvals.operations import Approval
    app = Approval(tmp_path)
    task = app.inspect(2)['task']
    assert task['status'] == 'reviewing'
    assert Path(task['material_file']).is_file()
    assert app.store.run(started['run_id'])['status'] == 'closed'
    assert not Path(started['socket']).exists()
    reopened = subprocess.run(command(tmp_path, 'pending', '--demo'), cwd=PROJECT,
                              input='{"operation":"finish"}\n', capture_output=True, text=True, timeout=15)
    assert reopened.returncode == 0, reopened.stdout + reopened.stderr
    assert json.loads(reopened.stdout)['run_id'] != started['run_id']


def test_collection_process_crash_marks_preparing_task_failed_once(blocked_collection, tmp_path):
    process, started, first, invoke, client, channel = blocked_collection
    channel.sendall(b'x')
    assert json.loads(client.makefile().readline()) == {'status':'failed', 'error':'collection_process_failed'}
    from apps.oa.approvals.operations import Approval
    app = Approval(tmp_path)
    task = app.inspect(2)['task']
    assert task['status'] == 'failed'
    assert task['failure_count'] == 1
    assert invoke('collect', '--agent-id', 'third', '--expected-task-id', '3', '--agent-workflow-key', 'general_expense')['task_id'] == 3
    process.stdin.close()
    process.wait(timeout=15)
    assert process.returncode == 0, process.stderr.read()


def test_socket_finish_waits_for_collection_and_refuses_new_claims(blocked_collection, tmp_path):
    import socket

    process, started, first, invoke, collector, channel = blocked_collection
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as finishing:
        finishing.settimeout(15)
        finishing.connect(started['socket'])
        finishing.sendall((json.dumps({'operation': 'finish', 'run_id': started['run_id']}) + '\n').encode())
        assert invoke('pending', '--demo')['status'] == 'already_running'
        refused = invoke('collect', '--agent-id', 'third', '--expected-task-id', '3',
                         '--agent-workflow-key', 'general_expense')
        assert refused == {'run_id': started['run_id'], 'status': 'blocked', 'reason': 'run_finishing'}
        channel.sendall(b'r')
        assert json.loads(collector.makefile().readline())['status'] == 'reviewing'
        assert json.loads(finishing.makefile().readline())['status'] == 'closed'
    process.wait(timeout=15)
    assert process.returncode == 0, process.stderr.read()
    status = invoke('status')
    assert status['run']['status'] == 'closed'
    assert status['summary'] == {'reviewing': 2, 'queued': 1}
    assert not Path(started['socket']).exists()


def test_all_finish_clients_receive_closed_after_collection_drains(blocked_collection):
    import socket

    process, started, first, invoke, collector, channel = blocked_collection
    finishers = [socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) for _ in range(2)]
    try:
        for client in finishers:
            client.settimeout(15)
            client.connect(started['socket'])
            client.sendall((json.dumps({'operation': 'finish', 'run_id': started['run_id']}) + '\n').encode())
            assert invoke('status')['owner']['reachable'] is True
        assert invoke('collect', '--agent-id', 'third', '--expected-task-id', '3',
                      '--agent-workflow-key', 'general_expense')['reason'] == 'run_finishing'
        channel.sendall(b'r')
        assert json.loads(collector.makefile().readline())['status'] == 'reviewing'
        closed = [json.loads(client.makefile().readline()) for client in finishers]
        assert closed[0] == closed[1]
        assert closed[0]['status'] == 'closed'
        assert closed[0]['run_id'] == started['run_id']
        assert all(client.recv(1) == b'' for client in finishers)
    finally:
        for client in finishers:
            client.close()
    process.wait(timeout=5)
    assert process.returncode == 0, process.stderr.read()
    assert not Path(started['socket']).exists()


def test_socket_finish_does_not_dispatch_stdin_collect_after_draining_worker(blocked_collection, tmp_path):
    import socket

    process, started, first, invoke, collector, channel = blocked_collection
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as finishing:
        finishing.settimeout(2)
        finishing.connect(started['socket'])
        finishing.sendall((json.dumps({'operation': 'finish', 'run_id': started['run_id']}) + '\n').encode())
        assert invoke('collect', '--agent-id', 'third', '--expected-task-id', '3',
                      '--agent-workflow-key', 'general_expense')['reason'] == 'run_finishing'
        process.stdin.write(json.dumps({'operation': 'collect', 'agent_id': 'third',
            'expected_task_id': 3, 'agent_workflow_key': 'general_expense'}) + '\n')
        process.stdin.flush()
        for _ in range(20):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.1)
                probe.connect(started['socket'])
                probe.sendall(b'{"operation":"status"}\n')
                try:
                    probe.recv(4096)
                except socket.timeout:
                    break
        else:
            pytest.fail('owner did not begin draining the blocked worker for stdin')
        channel.sendall(b'r')
        assert json.loads(collector.makefile().readline())['status'] == 'reviewing'
        assert json.loads(finishing.makefile().readline())['status'] == 'closed'
    process.wait(timeout=5)
    assert process.returncode == 0, process.stderr.read()
    assert process.stdout.read() == ''
    assert not Path(started['socket']).exists()
    status = invoke('status')
    assert status['run']['status'] == 'closed'
    assert status['summary'] == {'reviewing': 2, 'queued': 1}
    assert invoke('show', '--task-id', '3')['task']['status'] == 'queued'
    reopened = subprocess.run(command(tmp_path, 'start', '--demo'), cwd=PROJECT,
                              input='{"operation":"finish"}\n', capture_output=True, text=True, timeout=10)
    assert reopened.returncode == 0, reopened.stdout + reopened.stderr
    assert json.loads(reopened.stdout)['run_id'] != started['run_id']
