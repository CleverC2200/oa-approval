from __future__ import annotations

import json
import io
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from tests.approvals.source_fakes import POLICY


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
    monkeypatch.setattr(api, 'build_api', lambda _config, **kwargs: fake)
    monkeypatch.setattr(owner, 'require_production_main', lambda: None)
    monkeypatch.setattr(owner, 'load_source_policy', lambda: POLICY)
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


def test_production_round_lock_is_shared_across_runtime_roots_before_api_reads(
        tmp_path, monkeypatch, capsys, pending_output):
    from apps.oa.approvals import cli, owner
    from apps.oa.approvals.sources import oa as api

    monkeypatch.setattr(owner, 'PROJECT_ROOT', tmp_path / 'project')
    shared = owner.PROJECT_ROOT / 'runtime'
    second = tmp_path / 'different-runtime'
    with owner.round_lock(shared):
        with monkeypatch.context() as blocked:
            blocked.setattr(api, 'build_api', lambda *args, **kwargs: pytest.fail('OA must not be read'))
            assert cli.main(['--root',str(second),'start']) == 0
            assert json.loads(capsys.readouterr().out) == {'status':'already_running'}
            assert not second.exists()
    assert pending_output()['total'] == 23
    assert pending_output()['total'] == 23


def test_pending_request_filter_freezes_only_fresh_matching_requests(pending_output):
    started = pending_output('--request-id', '103', '--request-id', '101', '--request-id', '999')
    assert started['total'] == 2
    assert [task['request_id'] for task in started['tasks']] == ['101', '103']


@pytest.mark.parametrize('operation', ['submit', 'preview'])
@pytest.mark.parametrize('finance_policy', [False, True])
def test_pending_summary_keeps_stdin_collect_and_default_preview(tmp_path, pending_output, monkeypatch, capsys, operation, finance_policy):
    from apps.oa.approvals import cli, owner
    from apps.oa.approvals import finance_precheck
    from apps.oa.approvals.sources import auth, official_sql, prediction

    monkeypatch.setattr(finance_precheck, 'load_policy', lambda: {'leader': {'id': '100'}} if finance_policy else None)
    def unexpected(*args, **kwargs):
        pytest.fail('demo must not create a real prediction session, authenticate, or query SQL')
    monkeypatch.setattr(prediction, '_PredictionSession', unexpected)
    monkeypatch.setattr(auth, 'credentials', unexpected)
    monkeypatch.setattr(official_sql, '_query_rows', unexpected)

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
    assert preview.get('delivery', {}).get('action') == ('submit' if finance_policy else None)
    with sqlite3.connect(tmp_path / 'approval.sqlite3') as database:
        assert database.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0


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


def test_public_cli_handoff_starts_fresh_owners_until_preview_backlog_is_drained(tmp_path):
    arguments = ['start','--demo','--batch-size','1','--limit','0']
    opinion = tmp_path / 'opinion.txt'
    opinion.write_text('Synthetic preview')
    seen = []
    for _ in range(3):
        process = subprocess.Popen(command(tmp_path,*arguments),cwd=PROJECT,stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            started = json.loads(process.stdout.readline())
            assert started['status'] == 'ready'

            def invoke(*options):
                result = subprocess.run(command(tmp_path,*options),cwd=PROJECT,capture_output=True,
                                        text=True,timeout=20)
                assert result.returncode == 0, result.stdout + result.stderr
                return json.loads(result.stdout)

            status = invoke('status','--run-id',started['run_id'],'--view','batch')
            task = status['next_candidate']
            seen.append(task['id'])
            assert invoke('collect','--agent-id',f"reviewer-{started['run_id']}",
                          '--expected-task-id',str(task['id']),
                          '--agent-workflow-key',task['workflow_key'])['status'] == 'reviewing'
            assert invoke('preview','--task-id',str(task['id']),
                          '--opinion-file',str(opinion))['status'] == 'preview'
            closed = invoke('finish','--run-id',started['run_id'],'--handoff')
            process.wait(timeout=10)
            status = invoke('status','--run-id',started['run_id'],'--view','batch')
            assert status['run']['status'] == 'closed'
            assert status['owner']['reachable'] is False
            if closed['handoff']['status'] == 'done':
                break
            assert closed['handoff']['status'] == 'ready'
            arguments = [*closed['handoff']['start_arguments'],'--demo']
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
    assert len(set(seen)) == 3
    assert closed['handoff'] == {'status':'done','remaining':0}


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
def blocked_collection(tmp_path, request):
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
from apps.oa.approvals import cli, demo, owner
from apps.oa.runtime import collection_budget
owner.COLLECTION_TIMEOUT_SECONDS = COLLECTION_BUDGET
original_collect = demo.collect_demo

def collect(request_id, **options):
    if request_id == '90002':
        if PHASE_LIMIT:
            collection_budget.record_read({'status': READ_STATUS, 'observations': [
                {'diagnostics': [{'stage': 'browser', 'reason': 'browser_failed', 'exception_type': 'RuntimeError'}]}]})
            phase = collection_budget.phase('synthetic_browser_cleanup', 1)
            phase.__enter__()
        if owner.COLLECTION_TIMEOUT_SECONDS < 900:
            import subprocess
            descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'], start_new_session=True)
            Path(DETACHED_PID).write_text(str(descendant.pid))
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
'''.replace('PROJECT', repr(str(PROJECT))).replace('CONTROL', repr(str(control_path))).replace('UNKNOWN', repr(str(tmp_path / 'unknown'))).replace('COLLECTION_BUDGET', repr(900 if getattr(request, 'param', None) in {'phase', 'phase_complete'} else getattr(request, 'param', 900))).replace('DETACHED_PID', repr(str(tmp_path / 'detached-pid'))).replace('PHASE_LIMIT', repr(getattr(request, 'param', None) in {'phase', 'phase_complete'})).replace('READ_STATUS', repr('complete' if getattr(request, 'param', None) == 'phase_complete' else 'failed')))
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


@pytest.mark.parametrize('stop', ['eof', 'finish', 'sigterm'])
def test_exit_waits_for_disconnected_collection_and_keeps_lock(blocked_collection, tmp_path, stop):
    process, started, first, invoke, client, channel = blocked_collection
    client.close()
    if stop == 'finish':
        process.stdin.write('{"operation":"finish"}\n')
        process.stdin.flush()
    elif stop == 'sigterm':
        process.terminate()
    else:
        process.stdin.close()
    assert invoke('pending', '--demo')['status'] == 'already_running'
    channel.sendall(b'r')
    process.wait(timeout=15)
    assert process.returncode == (130 if stop == 'sigterm' else 0), process.stderr.read()
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
    result = json.loads(client.makefile().readline())
    assert result['status'] == 'failed' and result['error'] == 'collection_process_failed'
    assert result['task_id'] == 2 and result['stage'] == 'unknown'
    assert Path(result['diagnostics_file']).is_file()
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


@pytest.mark.parametrize('blocked_collection', [3], indirect=True)
@pytest.mark.parametrize('finish', [False, True])
def test_collection_deadline_releases_worker_and_allows_finish(blocked_collection, tmp_path, finish):
    import socket

    process, started, first, invoke, client, channel = blocked_collection
    before = invoke('status', '--view', 'batch')
    assert before['owner']['phase'] == 'running'
    assert before['owner']['collection']['timeout_seconds'] == 3
    assert before['owner']['collection']['elapsed_seconds'] >= 0
    finishing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    finishing.settimeout(10)
    try:
        if finish:
            finishing.connect(started['socket'])
            finishing.sendall((json.dumps({'operation': 'finish', 'run_id': started['run_id']}) + '\n').encode())
            assert invoke('status')['owner']['phase'] == 'finishing'
        result = json.loads(client.makefile().readline())
        assert result['status'] == 'failed' and result['error'] == 'collection_deadline'
        child_pid = int((tmp_path / 'detached-pid').read_text())
        child = subprocess.run(['ps', '-p', str(child_pid), '-o', 'stat='], capture_output=True, text=True)
        assert not child.stdout.strip() or child.stdout.strip().startswith('Z')
        facts = invoke('status', '--view', 'batch')
        assert facts['batch']['summary']['failed'] == 1
        assert facts['acceptance']['attempt_count'] == 0
        if finish:
            assert json.loads(finishing.makefile().readline())['status'] == 'closed'
        else:
            assert facts['owner']['collection'] is None
            assert invoke('finish', '--run-id', started['run_id'])['status'] == 'closed'
        process.wait(timeout=10)
        assert invoke('status')['owner']['reachable'] is False
    finally:
        finishing.close()


def _partial_collection_result(output):
    import os
    import struct
    import time

    os.setsid()
    os.write(output.fileno(), struct.pack('!i', 10000))
    time.sleep(60)


def test_partial_collection_reply_cannot_block_deadline(tmp_path, monkeypatch):
    import multiprocessing
    import time

    from apps.oa.approvals import owner
    from apps.oa.approvals.demo import create_demo

    app = create_demo(tmp_path)
    started = app.pending(batch_size=1)
    task = started['tasks'][0]
    app.store.claim(started['run_id'], agent_id='partial', expected_task_id=task['id'],
                    agent_workflow_key=task['workflow_key'])
    context = multiprocessing.get_context('spawn')
    incoming, output = context.Pipe(duplex=False)
    process = context.Process(target=_partial_collection_result, args=(output,))
    process.start()
    output.close()
    replies = []
    collection = owner._Collection(process, incoming, replies.append, app.store, started['run_id'])
    monkeypatch.setattr(owner, 'COLLECTION_TIMEOUT_SECONDS', 1)
    beginning = time.monotonic()
    try:
        assert incoming.poll(5)
        collection.complete()
        assert time.monotonic() - beginning < 6
        assert len(replies) == 1
        assert replies[0]['status'] == 'failed' and replies[0]['error'] == 'collection_deadline'
        assert replies[0]['task_id'] == task['id'] and replies[0]['stage'] == 'unknown'
        assert app.store.get_task(task['id'])['status'] == 'failed'
    finally:
        app.store.connection.close()


def test_process_scan_failure_does_not_leave_frozen_collector(tmp_path, monkeypatch):
    import multiprocessing

    from apps.oa.approvals import owner
    from apps.oa.approvals.demo import create_demo

    context = multiprocessing.get_context('spawn')
    incoming, output = context.Pipe(duplex=False)
    process = context.Process(target=_partial_collection_result, args=(output,))
    process.start()
    output.close()
    app = create_demo(tmp_path)
    collection = owner._Collection(process, incoming, lambda value: None, app.store, 'synthetic')
    try:
        assert incoming.poll(5)
        def failed_scan(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], 2)
        monkeypatch.setattr(owner.subprocess, 'run', failed_scan)
        with pytest.raises(subprocess.TimeoutExpired):
            collection.stop()
        assert not process.is_alive()
    finally:
        incoming.close()
        process.close()
        app.store.connection.close()


@pytest.mark.parametrize('blocked_collection,read_status', [('phase', 'failed'), ('phase_complete', 'complete')],
                         indirect=['blocked_collection'])
def test_browser_cleanup_deadline_does_not_wait_for_collection_total(blocked_collection, read_status):
    import time

    process, started, first, invoke, client, channel = blocked_collection
    beginning = time.monotonic()
    before = invoke('status')['owner']['collection']
    assert before['timeout_seconds'] == 900
    assert before['phase']['phase'] == 'synthetic_browser_cleanup'
    result = json.loads(client.makefile().readline())
    assert result['error'] == 'collection_deadline'
    assert result['stage'] == 'synthetic_browser_cleanup'
    assert result['task_id'] == 2 and result['run_id'] == started['run_id']
    assert result['agent_id'] == 'second'
    report = json.loads(Path(result['diagnostics_file']).read_text())
    assert report['stage'] == result['stage'] and report['task_id'] == 2
    reads = json.loads(Path(report['read_results_file']).read_text())
    assert reads['run_id'] == started['run_id'] and reads['task_id'] == 2
    assert reads['read_results'][0]['status'] == read_status
    assert 'observations' not in json.dumps(result)
    shown = invoke('show', '--task-id', '2')
    assert shown['collection_failure']['diagnostics_file'] == result['diagnostics_file']
    assert shown['collection_failure']['stage'] == result['stage']
    assert shown['task']['status'] == 'failed'
    assert shown['task']['material_file'] is None
    assert shown['submissions'] == []
    assert time.monotonic() - beginning < 6
    assert invoke('finish', '--run-id', started['run_id'])['status'] == 'closed'
    process.wait(timeout=10)
    assert invoke('status')['owner']['reachable'] is False
