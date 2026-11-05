from __future__ import annotations

import io
import json
from types import SimpleNamespace
import urllib.error
import urllib.parse

import pytest

from apps.oa.__main__ import main
from apps.oa.approvals.sources import prediction
from tests.approvals.source_fakes import POLICY


def result(*, complete=True):
    return {
        'forecastInfo': {'isEnd': complete, 'forecastUserName': '测试账号',
                         'forecastNodeName': '预审', 'forecastDate': '2026-01-01', 'forecastTime': '10:00:00'},
        'forecastLinkColor': '#123456',
        'nativeExtra': {'retained': ['a', 'b']},
        'nodeInfo': [
            {'nodeid': '9', 'nodeName': '发起', 'nodeType': 0},
            {'nodeid': '10', 'nodeName': '预审', 'nodeType': 1},
            {'nodeid': 20, 'nodeName': '财务', 'nodeType': 3, 'branchNumber': 2,
             'currentBranch': 1, 'nodeRemark': '20_1', 'nodeColor': '#654321'},
            {'nodeid': 20, 'nodeName': '财务', 'nodeType': 3, 'nodeRemark': '20_1'},
            {'nodeid': -2, 'nodeName': '', 'nodeType': 3},
        ],
    }


@pytest.fixture
def native(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(prediction, 'time', SimpleNamespace(
        monotonic=lambda: clock.now, sleep=lambda seconds: setattr(clock, 'now', clock.now + seconds)))
    values = {'OA_BASE_URL': POLICY.attachments.oa_origin, 'OA_UID': '1', 'OA_APP_ID': 'test-app'}
    monkeypatch.setattr(prediction.auth, 'env', lambda name: values.get(name, ''))
    authentications, requests, replies = [], [], []
    account = {'data': {'userid': '1'}}
    operators = {('9', '0'): {'nodes': [{'operators': [{'id': 4, 'name': '发起人', 'type': 0}]}]},
                 ('10', '1'): {'nodes': [{'operators': [{'id': 1, 'name': '测试账号', 'type': 0}]}]},
                 ('20', '3'): {'nodes': [{'operators': [
        {'id': 2, 'name': '甲', 'type': 0}, {'id': 3, 'name': '乙', 'type': 0}],
        'nodePoint': [1, 2, 3, 4]}]}}

    def credentials(*, base_url):
        authentications.append(base_url)
        return 'private-token', 'encrypted-user'

    class Response(io.BytesIO):
        headers = {}

    def urlopen(request, *, timeout):
        path = urllib.parse.urlsplit(request.full_url).path
        params = urllib.parse.parse_qs((request.data or b'').decode())
        requests.append((path, params, dict(request.header_items()), timeout))
        assert timeout > 0
        if path == prediction.ACCOUNT_PATH:
            payload = account
        elif path == prediction.OPERATOR_PATH:
            payload = operators[(params['cnodeid'][0], params['nodeType'][0])]
        else:
            payload = replies.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr(prediction.auth, 'credentials', credentials)
    monkeypatch.setattr(prediction.http, 'urlopen', urlopen)
    return SimpleNamespace(authentications=authentications, requests=requests, replies=replies,
                           account=account, operators=operators)


def test_public_cli_preserves_full_raw_responses_and_every_node(native, tmp_path, capsys):
    payload = result()
    native.replies.append(payload)
    assert main(['approvals', '--root', str(tmp_path), 'predict', '--request-id', '123', '--format', 'json']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'ok'
    assert output['complete'] is True and output['has_unknown_nodes'] is True
    assert output['session_source'] == 'api'
    assert native.authentications == [POLICY.attachments.oa_origin]
    assert [request[0] for request in native.requests] == [
        prediction.ACCOUNT_PATH, prediction.PREDICTION_PATH,
        prediction.OPERATOR_PATH, prediction.OPERATOR_PATH, prediction.OPERATOR_PATH]
    assert native.requests[1][1] == {'requestid': ['123']}
    assert [r[1] for r in native.requests[2:]] == [
        {'requestid': ['123'], 'cnodeid': ['9'], 'nodeType': ['0']},
        {'requestid': ['123'], 'cnodeid': ['10'], 'nodeType': ['1']},
        {'requestid': ['123'], 'cnodeid': ['20'], 'nodeType': ['3'], 'isForecastNode': ['1']},
    ]
    assert all(request[2]['Token'] == 'private-token' and request[2]['Userid'] == 'encrypted-user'
               and 'Cookie' not in request[2] for request in native.requests)
    assert [node['node_id'] for node in output['nodes']] == ['9', '10', '20', '20', '-2']
    assert [node['sequence'] for node in output['nodes']] == [1, 2, 3, 4, 5]
    assert [node['state'] for node in output['nodes']] == ['passed', 'current', 'predicted', 'predicted', 'unknown']
    assert output['nodes'][0]['operators'] == [{'id': '4', 'name': '发起人', 'type': 0}]
    assert output['nodes'][1]['operators'] == [{'id': '1', 'name': '测试账号', 'type': 0}]
    assert output['nodes'][2]['operators'] == output['nodes'][3]['operators'] == [
        {'id': '2', 'name': '甲', 'type': 0}, {'id': '3', 'name': '乙', 'type': 0}]
    assert output['nodes'][2]['branch_count'] == 2
    assert output['nodes'][4]['operators_loaded'] is False
    assert output['raw_prediction'] == payload
    assert output['raw_operators'] == [
        {'request': {'requestid': '123', 'cnodeid': node_id, 'nodeType': node_type,
                     **({'isForecastNode': 1} if node_type == 3 else {})},
         'response': native.operators[(node_id, str(node_type))]}
        for node_id, node_type in [('9', 0), ('10', 1), ('20', 3)]
    ]
    serialized = json.dumps(output)
    assert all(value not in serialized for value in ['private-token', 'encrypted-user'])
    assert all(value not in json.dumps(output['nodes']) for value in ['nodeColor', 'nodePoint', 'LinkColor'])
    assert list(tmp_path.iterdir()) == []


def test_refresh_polls_without_restarting_prediction_and_renders_people(native, capsys):
    native.replies.extend([result(complete=False), result()])
    assert main(['approvals', 'predict', '--request-id', '123', '--refresh']) == 0
    output = capsys.readouterr().out
    assert '[已经过] 发起 (9) · 发起人 (4)' in output
    assert '[当前] 预审 (10) · 测试账号 (1)' in output
    assert '甲 (2)、乙 (3) · 分支 1/2' in output
    assert '后续节点无法预知' in output
    assert output.count('[预测] 财务 (20)') == 2
    assert '[未知] 后续节点无法预知' in output
    assert [r[1] for r in native.requests if r[0] == prediction.PREDICTION_PATH] == [
        {'requestid': ['123'], 'resetForecast': ['1']}, {'requestid': ['123']},
    ]


def test_same_node_in_history_and_prediction_uses_its_own_operator_context(native):
    payload = result()
    payload['nodeInfo'][0]['nodeid'] = 20
    native.operators[('20', '0')] = {'nodes': [{'operators': [{'id': 8, 'name': '历史办理人', 'type': 0}]}]}
    native.replies.append(payload)
    output = prediction.predict('123')
    assert output['nodes'][0]['operators'][0]['id'] == '8'
    assert output['nodes'][2]['operators'][0]['id'] == '2'
    assert output['nodes'][3]['operators'] == output['nodes'][2]['operators']
    assert [r[1]['nodeType'] for r in native.requests
            if r[0] == prediction.OPERATOR_PATH and r[1]['cnodeid'] == ['20']] == [['0'], ['3']]
    assert len(output['raw_operators']) == 3


def test_unrecognized_native_states_and_placeholders_remain_unknown_without_operator_guesses(native):
    payload = result()
    payload['nodeInfo'].extend([
        {'nodeid': 99, 'nodeName': '未识别状态', 'nodeType': 2},
        {'nodeid': -3, 'nodeName': '新增占位', 'nodeType': 3},
    ])
    native.replies.append(payload)
    output = prediction.predict('123')
    assert output['raw_prediction'] == payload
    assert [node['node_id'] for node in output['nodes'][-2:]] == ['99', '-3']
    assert all(node['state'] == 'unknown' and not node['operators_loaded'] for node in output['nodes'][-2:])
    assert len(native.requests) == 5


def test_timeout_returns_partial_path_with_failure_exit(native, capsys):
    pending = result(complete=False)
    pending['nodeInfo'].append({'nodeid': -1, 'nodeName': '智能预测中', 'nodeType': 3})
    native.replies.append(pending)
    assert main(['approvals', 'predict', '--request-id', '123', '--timeout', '1', '--format', 'json']) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'timeout' and output['complete'] is False
    assert output['nodes'][-1]['state'] == 'loading'
    assert not any(node['operators_loaded'] for node in output['nodes'])
    assert output['raw_prediction'] == pending
    assert output['raw_operators'] == []
    assert len(native.requests) == 2


def test_people_read_timeout_keeps_unread_nodes_distinct_from_empty_people(native, capsys):
    native.replies.append(result())
    native.operators[('20', '3')] = TimeoutError()
    assert main(['approvals', 'predict', '--request-id', '123', '--format', 'json']) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['complete'] is False and output['forecast']['isEnd'] is True
    assert all(node['operators_loaded'] for node in output['nodes'][:2])
    assert all(not node['operators_loaded'] for node in output['nodes'][2:])
    assert len(output['raw_operators']) == 2


def test_wrapped_connection_timeout_preserves_people_already_read(native, capsys):
    payload = result()
    payload['nodeInfo'][3:3] = [
        {'nodeid': 30, 'nodeName': '复核', 'nodeType': 3},
        {'nodeid': 40, 'nodeName': '终审', 'nodeType': 3},
    ]
    native.replies.append(payload)
    native.operators[('30', '3')] = urllib.error.URLError(TimeoutError('private-timeout-message'))
    assert main(['approvals', 'predict', '--request-id', '123', '--format', 'json']) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'timeout' and output['complete'] is False
    assert all(node['operators_loaded'] for node in output['nodes'][:3])
    assert output['nodes'][2]['operators'][0]['id'] == '2'
    assert all(not node['operators_loaded'] for node in output['nodes'][3:])
    assert output['raw_prediction'] == payload
    assert len(output['raw_operators']) == 3
    assert 'private-timeout-message' not in json.dumps(output)
    assert [r[1]['cnodeid'] for r in native.requests if r[0] == prediction.OPERATOR_PATH] == [['9'], ['10'], ['20'], ['30']]


def test_identity_mismatch_stops_before_prediction(native, capsys):
    native.account['data']['userid'] = '2'
    assert main(['approvals', 'predict', '--request-id', '123']) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'prediction_session_identity_mismatch'
    assert len(native.requests) == 1


@pytest.mark.parametrize('payload', [
    {'status': False, 'message': 'private-server-message'}, [], {},
    {'forecastInfo': {'isEnd': 'true'}, 'nodeInfo': [{}]},
    {'forecastInfo': {'isEnd': True}, 'nodeInfo': []},
    {'forecastInfo': {'isEnd': True}, 'nodeInfo': ['bad']},
])
def test_denied_or_malformed_prediction_is_not_success(native, payload, capsys):
    native.replies.append(payload)
    assert main(['approvals', 'predict', '--request-id', '123', '--format', 'json']) == 1
    assert json.loads(capsys.readouterr().out) == {'status': 'failed', 'error': 'prediction_response_invalid'}
    assert len(native.requests) == 2


@pytest.mark.parametrize('operators', [{}, {'nodes': []}, {'nodes': [{'operators': ['bad']}]},
                                        {'nodes': [{'operators': [{'id': 2, 'name': ''}]}]}])
def test_missing_or_malformed_people_response_is_not_empty_success(native, operators, capsys):
    native.replies.append(result())
    native.operators[('20', '3')] = operators
    assert main(['approvals', 'predict', '--request-id', '123']) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'prediction_operators_invalid'


@pytest.mark.parametrize('error', [urllib.error.HTTPError('https://example.test/private', 403,
                                                         'private-server-message', {}, None),
                                 urllib.error.URLError('private-server-message')])
def test_http_failure_does_not_retry_or_expose_response(native, capsys, error):
    native.replies.append(error)
    assert main(['approvals', 'predict', '--request-id', '123']) == 1
    assert json.loads(capsys.readouterr().out) == {'status': 'failed', 'error': type(error).__name__}
    assert len(native.requests) == 2


@pytest.mark.parametrize('request_id, timeout', [('0', 60), ('-1', 60), ('123&resetForecast=1', 60),
                                              ('123', 0), ('123', 301)])
def test_invalid_arguments_do_not_authenticate(native, request_id, timeout):
    with pytest.raises(ValueError):
        prediction.predict(request_id, timeout=timeout)
    assert not native.authentications and not native.requests
