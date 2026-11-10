from __future__ import annotations

import io
import json
from types import SimpleNamespace
import urllib.error
import urllib.parse

import pytest

from apps.oa.__main__ import main
from apps.oa.approvals import finance_precheck
from apps.oa.approvals.sources import prediction
from tests.approvals.source_fakes import POLICY


def result(*, complete=True):
    return {
        'forecastInfo': {'isEnd': complete, 'forecastUserName': '测试账号',
                         'forecastNodeName': '预审', 'forecastDate': '2026-01-01', 'forecastTime': '10:00:00'},
        'forecastLinkColor': '#123456',
        'nativeExtra': {'retained': ['a', 'b']},
        'nodeInfo': [
            {'nodeid': '9', 'nodeName': '发起', 'nodeType': 0, 'ntype': '0'},
            {'nodeid': '10', 'nodeName': '预审', 'nodeType': 1, 'ntype': '1'},
            {'nodeid': 20, 'nodeName': '财务', 'nodeType': 3, 'ntype': '1', 'branchNumber': 2,
             'currentBranch': 1, 'nodeRemark': '20_1', 'nodeColor': '#654321'},
            {'nodeid': 20, 'nodeName': '财务', 'nodeType': 3, 'ntype': '1', 'nodeRemark': '20_1'},
            {'nodeid': -2, 'nodeName': '', 'nodeType': 3},
        ],
    }


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(finance_precheck, 'load_policy', lambda: None)
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(prediction, 'time', SimpleNamespace(
        monotonic=lambda: clock.now, sleep=lambda seconds: setattr(clock, 'now', clock.now + seconds)))
    values = {'OA_BASE_URL': POLICY.attachments.oa_origin, 'OA_UID': '1', 'OA_APP_ID': 'test-app'}
    monkeypatch.setattr(prediction.auth, 'env', lambda name: values.get(name, ''))
    authentications, requests, replies, urls = [], [], [], []
    account = {'data': {'userid': '1'}}
    request_details = {'payload': {'code': 'SUCCESS', 'data': {
        'requestId': '123', 'creatorId': '9', 'creatorName': '名单外发起人', 'canView': True}}}
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
        urls.append(request.full_url)
        path = urllib.parse.urlsplit(request.full_url).path
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query or (request.data or b'').decode())
        requests.append((path, params, dict(request.header_items()), timeout))
        assert timeout > 0
        if path == prediction.ACCOUNT_PATH:
            payload = account
        elif path == prediction.REQUEST_PATH:
            assert request.get_method() == 'GET' and request.data is None
            payload = request_details['payload']
        elif path == prediction.OPERATOR_PATH:
            payload = operators[(params['cnodeid'][0], params['nodeType'][0])]
        else:
            payload = replies.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr(prediction.auth, 'credentials', credentials)
    monkeypatch.setattr(prediction.http, 'urlopen', urlopen)
    return SimpleNamespace(authentications=authentications, requests=requests, replies=replies, urls=urls, env=values,
                           account=account, operators=operators, request_details=request_details)


@pytest.mark.parametrize('identity_matches', [False, True])
def test_explicit_origin_controls_authentication_and_every_prediction_read(native, identity_matches):
    native.env['OA_BASE_URL'] = 'https://unused.example.test'
    native.account['data']['userid'] = '1' if identity_matches else '2'
    native.replies.append(result())
    if identity_matches:
        assert prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)['complete'] is True
    else:
        with pytest.raises(RuntimeError, match='prediction_session_identity_mismatch'):
            prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)
        assert len(native.requests) == 1
    assert native.authentications == [POLICY.attachments.oa_origin]
    assert all(url.startswith(POLICY.attachments.oa_origin + '/') for url in native.urls)


def test_approval_default_prediction_uses_its_source_policy(native, finance_rule, tmp_path):
    from tests.approvals.test_operations import service

    native.env['OA_BASE_URL'] = 'https://unused.example.test'
    native.request_details['payload']['data']['requestId'] = '101'
    native.replies.append(result())
    app, api = service(tmp_path)
    try:
        task = app.collect(app.pending()['run_id'], agent_id='reviewer', expected_task_id=1,
                           agent_workflow_key='travel_expense')
        preview = app.submit(task['task_id'], opinion='合成预览')
        assert preview['status'] == 'preview'
        assert native.authentications == [POLICY.attachments.oa_origin]
        assert all(url.startswith(POLICY.attachments.oa_origin + '/') for url in native.urls)
        assert native.requests[2][1] == {'requestid': ['101'], 'resetForecast': ['1']}
        assert api.posts == [] and app.inspect(task['task_id'])['submissions'] == []
    finally:
        app.store.connection.close()


def test_public_cli_preserves_full_raw_responses_and_every_node(native, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('OA_TRAVEL_BROWSER_ENGINE', 'unused-browser')
    monkeypatch.setenv('OA_TRAVEL_SESSION_SOURCE', 'unused-session')
    payload = result()
    native.replies.append(payload)
    assert main(['approvals', '--root', str(tmp_path), 'predict', '--request-id', '123', '--format', 'json']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'ok'
    assert output['complete'] is True and output['has_unknown_nodes'] is True
    assert output['session_source'] == 'api'
    assert output['finance_precheck']['status'] == 'not_configured'
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
    output = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)
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
    output = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)
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
        prediction.predict(request_id, oa_origin=POLICY.attachments.oa_origin, timeout=timeout)
    assert not native.authentications and not native.requests


@pytest.fixture
def finance_rule(native, monkeypatch):
    policy = {'leader': {'id': '100', 'name': '测试高层'},
              'initiators': [{'id': '4', 'name': '名单内发起人'}]}
    monkeypatch.setattr(finance_precheck, 'load_policy', lambda: policy)
    return policy


@pytest.mark.parametrize('initiator_hit, route_hit', [(True, False), (False, True), (True, True), (False, False)])
def test_finance_precheck_or_conditions_through_public_cli(native, finance_rule, initiator_hit, route_hit, capsys):
    payload = result()
    payload['nodeInfo'].pop()
    native.replies.append(payload)
    if initiator_hit:
        native.request_details['payload']['data'].update(creatorId='4', creatorName='名单内发起人')
    if route_hit:
        native.operators[('20', '3')]['nodes'][0]['operators'].append({'id': 100, 'name': '测试高层', 'type': 0})
    assert main(['approvals', 'predict', '--request-id', '123', '--format', 'json']) == 0
    output = json.loads(capsys.readouterr().out)
    assessment = output['finance_precheck']
    assert assessment['required'] is (initiator_hit or route_hit)
    assert assessment['status'] == ('required' if initiator_hit or route_hit else 'not_required')
    codes = {reason['code'] for reason in assessment['reasons']}
    assert ('initiator_in_list' in codes) is initiator_hit
    assert ('future_approver_match' in codes) is route_hit
    assert output['raw_prediction'] == payload
    assert output['initiator']['id'] == ('4' if initiator_hit else '9')
    assert native.requests[1][0:2] == (prediction.REQUEST_PATH, {'requestId': ['123']})
    if route_hit:
        assert [reason['sequence'] for reason in assessment['reasons']
                if reason['code'] == 'future_approver_match'] == [3, 4]


@pytest.mark.parametrize('node_type, kind, person_type, expected', [
    (0, '1', 0, False), (1, '1', 0, False), (3, '3', 0, False),
    (3, '5', 0, False), (3, '6', 0, False), (3, '1', 2, False),
    (3, '1', 1, False), (3, '1', None, None), (3, '1', 99, None), (3, None, 0, None), (3, '2', 0, True),
])
def test_finance_matches_only_future_human_approval_or_processing_nodes(native, finance_rule,
                                                                      node_type, kind, person_type, expected):
    payload = result()
    payload['nodeInfo'] = [{'nodeid': 20, 'nodeName': '目标节点', 'nodeType': node_type, 'ntype': kind}]
    native.operators[('20', str(node_type))] = {'nodes': [{'operators': [
        {'id': 100, 'name': '测试高层', 'type': person_type}]}]}
    native.replies.append(payload)
    output = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)
    assert output['finance_precheck']['required'] is expected


@pytest.mark.parametrize('initiator_hit', [False, True])
def test_unknown_route_is_not_false_but_initiator_match_still_requires_finance(native, finance_rule, initiator_hit):
    native.replies.append(result())
    if initiator_hit:
        native.request_details['payload']['data'].update(creatorId='4', creatorName='名单内发起人')
    assessment = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)['finance_precheck']
    assert assessment['required'] is (True if initiator_hit else None)
    assert 'prediction_path_unknown' in assessment['uncertainties']


@pytest.mark.parametrize('route_hit', [False, True])
def test_initiator_read_failure_does_not_hide_independent_future_approver_match(native, finance_rule, route_hit):
    payload = result()
    payload['nodeInfo'].pop()
    native.replies.append(payload)
    native.request_details['payload'] = urllib.error.HTTPError('https://example.test/private', 403,
                                                              'private-server-message', {}, None)
    if route_hit:
        native.operators[('20', '3')]['nodes'][0]['operators'].append({'id': 100, 'name': '测试高层', 'type': 0})
    output = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)
    assert output['complete'] is True and output['initiator']['read_status'] == 'unavailable'
    assert output['finance_precheck']['required'] is (True if route_hit else None)
    assert 'private-server-message' not in json.dumps(output)


@pytest.mark.parametrize('details', [
    {'code': 'ERROR', 'data': {'requestId': '123', 'creatorId': '4'}},
    {'code': 'SUCCESS', 'data': {'requestId': '999', 'creatorId': '4'}},
    {'code': 'SUCCESS', 'data': {'requestId': '123', 'creatorId': '4', 'canView': False}},
    {'code': 'SUCCESS', 'data': {'requestId': '123', 'creatorName': '名单内发起人'}},
    {'code': 'SUCCESS', 'data': {'requestId': '123', 'creatorId': '9'}},
    {'code': 'SUCCESS', 'data': {'requestId': '123', 'creatorId': '9', 'canView': None}},
    {'code': 'SUCCESS', 'data': {'requestId': '123', 'creatorId': '9', 'canView': 0}},
    {'code': 'SUCCESS', 'data': {'requestId': '123', 'creatorId': '9', 'canView': 'false'}},
])
def test_initiator_requires_valid_response_and_matching_request_identity(native, finance_rule, details):
    payload = result()
    payload['nodeInfo'].pop()
    native.replies.append(payload)
    native.request_details['payload'] = details
    output = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)
    assert output['finance_precheck']['required'] is None
    assert output['initiator']['read_status'] == 'unavailable'


def test_initiator_hit_is_retained_when_prediction_times_out(native, finance_rule, capsys):
    native.request_details['payload']['data'].update(creatorId='4', creatorName='名单内发起人')
    native.replies.append(result(complete=False))
    assert main(['approvals', 'predict', '--request-id', '123', '--timeout', '1']) == 1
    output = capsys.readouterr().out
    assert '需要财务前置' in output and '名单内发起人 (4)' in output


def test_empty_future_people_is_unknown_not_negative(native, finance_rule):
    payload = result()
    payload['nodeInfo'].pop()
    native.replies.append(payload)
    native.operators[('20', '3')]['nodes'][0]['operators'] = []
    assessment = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)['finance_precheck']
    assert assessment['required'] is None
    assert 'future_operators_unavailable' in assessment['uncertainties']


def test_unknown_operator_namespace_cannot_prove_a_negative_match(native, finance_rule):
    payload = result()
    payload['nodeInfo'].pop()
    native.replies.append(payload)
    native.operators[('20', '3')]['nodes'][0]['operators'] = [{'id': 200, 'name': '未知类型', 'type': 99}]
    assessment = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)['finance_precheck']
    assert assessment['required'] is None
    assert 'operator_type_unknown' in assessment['uncertainties']


def test_public_cli_reports_transfer_target_without_writing(native, finance_rule, capsys):
    payload = result()
    payload['nodeInfo'] = payload['nodeInfo'][:3]
    payload['nodeInfo'][2].update(nodeName='财务收单', branchNumber=1)
    native.request_details['payload']['data'].update(creatorId='4', creatorName='名单内发起人')
    native.operators[('20', '3')] = {'nodes': [{'operators': [{'id': 2, 'name': '合成财务', 'type': 0}]}]}
    native.replies.append(payload)
    assert main(['approvals', 'predict', '--request-id', '123', '--format', 'json']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['delivery']['action'] == 'transfer'
    assert output['delivery']['recipient_id'] == '2'
    assert output['delivery']['finance_node_sequence'] == 3
    assert output['raw_prediction'] == payload
    assert {row[0] for row in native.requests} <= {
        prediction.ACCOUNT_PATH, prediction.REQUEST_PATH, prediction.PREDICTION_PATH, prediction.OPERATOR_PATH}


@pytest.mark.parametrize('topology', ['closed', 'extra_exit', 'missing_exit', 'wrong_request', 'subprocess', 'not_archive', 'unavailable', 'other_unknown'])
def test_verified_terminal_loop_resolves_people_scope_without_hiding_native_unknown(native, finance_rule, monkeypatch, topology):
    from apps.oa.approvals.sources import official_sql

    payload = result()
    payload['nodeInfo'][2]['branchNumber'] = 1
    if topology == 'other_unknown':
        payload['nodeInfo'].insert(1, {'nodeid': 99, 'nodeType': 2, 'nodeName': '另一未知段'})
    payload['nodeInfo'].append({'nodeid': 30, 'nodeName': '归档', 'nodeType': 3, 'ntype': '3'})
    native.operators[('30', '3')] = {'nodes': [{'operators': []}]}
    native.replies.append(payload)
    rows = [
        {'requestid': 123, 'nodeid': 20, 'destnodeid': dest, 'isend': end, 'isreject': '0',
         'customworkflowid': None, 'node_type': kind}
        for dest, end, kind in [(20, '0', '1'), (30, '1', '3')]]
    if topology == 'extra_exit':
        rows.append({**rows[0], 'destnodeid': 40})
    elif topology == 'missing_exit':
        rows.pop()
    elif topology == 'wrong_request':
        rows[0]['requestid'] = 456
    elif topology == 'subprocess':
        rows[0]['customworkflowid'] = 99
    elif topology == 'not_archive':
        rows[1]['isend'] = '0'
    def read(name, params, **kwargs):
        if name == 'prediction_finance_tail_links':
            return []
        assert name == 'prediction_terminal_loop_links'
        assert params == {'request_id': 123, 'node_id': 20}
        if topology == 'unavailable':
            raise RuntimeError('official_sql_unavailable')
        return rows
    monkeypatch.setattr(official_sql, '_query_rows', read)
    output = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)
    assert output['raw_prediction'] == payload
    assert output['has_unknown_nodes'] is True
    assert output['nodes'][-2]['state'] == 'unknown'
    assert output['finance_precheck']['required'] is (False if topology == 'closed' else None)
    assert output['delivery']['action'] == ('submit' if topology == 'closed' else 'blocked')


@pytest.mark.parametrize('change', ['closed', 'leader', 'empty_people', 'unknown_person_type',
                                  'dead_end', 'wrong_request', 'subprocess', 'custom_workflow',
                                  'unknown_kind', 'row_limit', 'initiator'])
def test_verified_finance_tail_only_proves_no_additional_leader_match(native, finance_rule, monkeypatch, change):
    from apps.oa.approvals.sources import official_sql

    payload = result()
    payload['nodeInfo'] = payload['nodeInfo'][:3] + [
        {'nodeid': -2, 'nodeName': '', 'nodeType': 3},
        {'nodeid': 30, 'nodeName': '归档', 'nodeType': 3, 'ntype': '3'}]
    payload['nodeInfo'][2]['branchNumber'] = 0
    native.replies.append(payload)
    native.operators[('30', '3')] = {'nodes': [{'operators': []}]}
    native.operators[('50', '3')] = {'nodes': [{'operators': [{'id': 8, 'name': '后段办理', 'type': 1}]}]}
    definitions = {20: ('已知财务', '1', '0'), 30: ('归档', '3', '1'),
                   40: ('业务处理', '6', '0'), 50: ('后段办理', '2', '0')}
    rows = []
    for index, (source, dest) in enumerate([(20, 40), (40, 30), (40, 50), (50, 50), (50, 30)], 1):
        name, kind, end = definitions[source]
        dest_name, dest_kind, dest_end = definitions[dest]
        rows.append({'requestid': 123, 'workflowid': 456, 'linkid': index, 'nodeid': source,
                     'node_name': name, 'node_type': kind, 'node_isend': end,
                     'destnodeid': dest, 'destnode_name': dest_name, 'destnode_type': dest_kind,
                     'destnode_isend': dest_end, 'isreject': '0', 'customworkflowid': None,
                     'subworkflow_count': 0})
    if change == 'leader':
        native.operators[('50', '3')]['nodes'][0]['operators'] = [{'id': 100, 'name': '测试高层', 'type': 0}]
    elif change == 'empty_people':
        native.operators[('50', '3')]['nodes'][0]['operators'] = []
    elif change == 'unknown_person_type':
        native.operators[('50', '3')]['nodes'][0]['operators'][0]['type'] = 99
    elif change == 'dead_end':
        rows.pop()
    elif change == 'wrong_request':
        rows[0]['requestid'] = 999
    elif change == 'subprocess':
        rows[0]['subworkflow_count'] = 1
    elif change == 'custom_workflow':
        rows[0]['customworkflowid'] = 999
    elif change == 'unknown_kind':
        rows[0]['destnode_type'] = '4'
    elif change == 'row_limit':
        rows *= 52
    elif change == 'initiator':
        native.request_details['payload']['data'].update(creatorId='4', creatorName='名单内发起人')
    def read(name, params, **kwargs):
        assert name == 'prediction_finance_tail_links'
        assert params == {'request_id': 123}
        return rows
    monkeypatch.setattr(official_sql, '_query_rows', read)
    output = prediction.predict('123', oa_origin=POLICY.attachments.oa_origin)
    assert output['complete'] is True and output['has_unknown_nodes'] is True
    assert output['raw_prediction'] == payload
    assert output['nodes'][-2]['state'] == 'unknown'
    expected = False if change == 'closed' else True if change == 'initiator' else None
    assert output['finance_precheck']['required'] is expected
    assert output['delivery']['action'] == ('submit' if change == 'closed' else 'blocked')
    if change == 'closed':
        proof = output['nodes'][-2]['resolution']
        assert proof['kind'] == 'finance_tail_excludes_leader'
        assert {n['node_id'] for n in proof['nodes']} == {'20', '30', '40', '50'}
        assert proof['leader_id'] == '100'
        changed_policy = {**finance_rule, 'leader': {'id': '999', 'name': '新名单'}}
        assert finance_precheck.assess(output, changed_policy)['required'] is None
        assert finance_precheck.unresolved_path(output['nodes'][-2]) is True
