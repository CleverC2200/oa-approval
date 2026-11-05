from __future__ import annotations

from copy import deepcopy
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
        'nodeInfo': [
            {'nodeid': '10', 'nodeName': '预审', 'nodeType': 1,
             'nodeOperatorName': '0_#WFSPSTR_OPTTP#_1_#WFSPSTR_OPTTP#_测试账号'},
            {'nodeid': 20, 'nodeName': '财务', 'nodeType': 3, 'branchNumber': 2,
             'currentBranch': 1, 'nodeRemark': '20_1',
             'nodeOperatorName': '0_#WFSPSTR_OPTTP#_2_#WFSPSTR_OPTTP#_甲->0_#WFSPSTR_OPTTP#_3_#WFSPSTR_OPTTP#_乙'},
            {'nodeid': 20, 'nodeName': '财务', 'nodeType': 3, 'nodeRemark': '20_1'},
            {'nodeid': -2, 'nodeName': '', 'nodeType': 3},
        ],
    }


@pytest.fixture
def native(monkeypatch):
    clock = SimpleNamespace(now=0)
    prediction_time = SimpleNamespace(monotonic=lambda: clock.now,
                                     sleep=lambda seconds: setattr(clock, 'now', clock.now + seconds))
    monkeypatch.setattr(prediction, 'time', prediction_time)
    monkeypatch.setattr(prediction, 'load_source_policy', lambda: POLICY)
    monkeypatch.setattr(prediction.auth, 'env', lambda name: '1' if name == 'OA_UID' else '')
    captures, requests, replies = [], [], []
    account = {'data': {'userid': '1'}}

    def capture(endpoint, **kwargs):
        captures.append((endpoint, kwargs))
        return {'cookies': [{'name': 'session', 'value': 'private-cookie'}]}

    def open_request(request, *, timeout):
        path = urllib.parse.urlsplit(request.full_url).path
        requests.append((path, urllib.parse.parse_qs((request.data or b'').decode()),
                         dict(request.header_items()), timeout))
        assert timeout > 0
        payload = account if path == prediction.ACCOUNT_PATH else replies.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return io.BytesIO(json.dumps(payload).encode())

    class Response(io.BytesIO):
        headers = {}

    def urlopen(request, *, timeout):
        content = open_request(request, timeout=timeout)
        return Response(content.read())

    monkeypatch.setattr(prediction.browser_session, 'capture_storage_state', capture)
    monkeypatch.setattr(prediction.http, 'urlopen', urlopen)
    return SimpleNamespace(captures=captures, requests=requests, replies=replies, account=account)


def test_public_cli_uses_rpa_identity_and_preserves_native_path(native, tmp_path, capsys):
    payload = result()
    native.replies.append(payload)
    assert main(['approvals', '--root', str(tmp_path), 'predict', '--request-id', '123', '--format', 'json']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'ok'
    assert output['complete'] is True and output['has_unknown_nodes'] is True
    assert output['session_source'] == 'rpa'
    assert output['data'] == payload
    assert native.captures[0][0] == POLICY.browser.cdp_endpoint
    assert native.captures[0][1]['allowed_hosts'] == {urllib.parse.urlsplit(POLICY.attachments.oa_origin).hostname}
    assert [request[0] for request in native.requests] == [prediction.ACCOUNT_PATH, prediction.PREDICTION_PATH]
    assert native.requests[-1][1] == {'requestid': ['123']}
    assert 'private-cookie' not in json.dumps(output)
    assert list(tmp_path.iterdir()) == []


def test_refresh_polls_without_restarting_prediction_and_renders_operators(native, capsys):
    native.replies.extend([result(complete=False), result()])
    assert main(['approvals', 'predict', '--request-id', '123', '--refresh']) == 0
    output = capsys.readouterr().out
    assert '[当前] 预审' in output
    assert '甲 → 乙 · 分支 1/2' in output
    assert '后续节点无法预知' in output
    assert output.count('[预测] 财务') == 2
    assert [r[1] for r in native.requests[1:]] == [
        {'requestid': ['123'], 'resetForecast': ['1']}, {'requestid': ['123']},
    ]


def test_timeout_returns_partial_path_with_failure_exit(native, capsys):
    pending = result(complete=False)
    pending['nodeInfo'].append({'nodeid': -1, 'nodeName': '智能预测中', 'nodeType': 3})
    native.replies.append(pending)
    assert main(['approvals', 'predict', '--request-id', '123', '--timeout', '1', '--format', 'json']) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'timeout' and output['complete'] is False
    assert output['data'] == pending
    assert len(native.requests) == 2


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
def test_denied_or_malformed_response_is_not_success(native, payload, capsys):
    native.replies.append(payload)
    assert main(['approvals', 'predict', '--request-id', '123', '--format', 'json']) == 1
    assert json.loads(capsys.readouterr().out) == {'status': 'failed', 'error': 'prediction_response_invalid'}
    assert len(native.requests) == 2


def test_http_failure_does_not_retry_or_expose_response(native, capsys):
    native.replies.append(urllib.error.HTTPError('https://example.test/private', 403,
                                               'private-server-message', {}, None))
    assert main(['approvals', 'predict', '--request-id', '123']) == 1
    assert json.loads(capsys.readouterr().out) == {'status': 'failed', 'error': 'HTTPError'}
    assert len(native.requests) == 2


@pytest.mark.parametrize('request_id, timeout', [('0', 60), ('-1', 60), ('123&resetForecast=1', 60),
                                              ('123', 0), ('123', 301)])
def test_invalid_arguments_do_not_access_session(native, request_id, timeout):
    with pytest.raises(ValueError):
        prediction.predict(request_id, timeout=timeout)
    assert not native.captures and not native.requests


def test_completed_flag_does_not_hide_server_loading_or_unknown_nodes(native):
    payload = deepcopy(result())
    payload['nodeInfo'].append({'nodeid': -1, 'nodeType': 3})
    native.replies.append(payload)
    output = prediction.predict('123')
    assert output['complete'] and output['has_unknown_nodes']
