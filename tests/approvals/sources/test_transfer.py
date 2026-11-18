from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from apps.oa.approvals.sources import http, transfer, writer
from apps.oa.approvals.sources.travel_read_auth import ACCOUNT_PATH


DELIVERY = {'action': 'transfer', 'source_node_id': '20', 'finance_node_id': '30',
            'recipient_id': '8', 'recipient_name': '合成财务'}
NOW = datetime.now(timezone.utc).isoformat()


class TransferApi:
    def __init__(self):
        self.rows = [{'id': 100, 'userid': 7, 'userType': 0, 'nodeid': 20, 'isremark': 0}]
        self.logs = []
        self.pending = [{'request_id': '101'}]
        self.workflow = {'requestId': '101', 'currentNodeId': 20, 'canView': True, 'canEdit': True}
        self.status = {'requestId': '101', 'currentNodeId': 20, 'currentNodeType': '1',
                       'currentOperateId': 100, 'isremark': 0}

    def get(self, path, params=None):
        if path == ACCOUNT_PATH:
            return {'data': {'userid': '7'}}
        data = {transfer.REQUEST_PATH: self.workflow, transfer.OPERATORS_PATH: self.rows,
                transfer.LOGS_PATH: self.logs, '/api/workflow/paService/getRequestStatus': self.status}[path]
        return {'code': 'SUCCESS', 'data': deepcopy(data)}

    def pending_rows(self):
        return self.pending

    def transferred(self):
        local = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S').split()
        self.rows[0]['isremark'] = 'h'
        self.rows[0].update(operatedate=local[0], operatetime=local[1])
        self.rows.append({'id': 101, 'userid': 8, 'userType': 0, 'nodeid': 20, 'isremark': 0,
                          'receivedate': local[0], 'receivetime': local[1]})
        self.logs.append({'id': 201, 'operator': 7, 'operatortype': 0, 'nodeid': 20,
                          'receivedPersonids': '8', 'operatedate': local[0], 'operatetime': local[1],
                          'remarkHtml': '合成意见<br/>金额 &lt; 100'})
        self.pending = []


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv('OA_UID', '7')
    return TransferApi()


def test_transfer_readback_requires_new_recipient_and_exact_opinion(api):
    context = transfer.prepare(api, '101', DELIVERY)
    assert transfer.verify(api, '101', '合成意见\n金额 < 100', NOW, context)['postcheck_done_hit'] is False
    api.transferred()
    result = transfer.verify(api, '101', '合成意见\n金额 < 100', NOW, context)
    assert result['postcheck_done_hit'] is True
    assert result['submit_ok'] is True
    assert transfer.verify(api, '101', '另一份意见', NOW, context)['done_hit'] is False


@pytest.mark.parametrize('source_time_missing', [False, True])
def test_transfer_readback_accepts_dated_transfer_log_when_source_time_is_blank(api, source_time_missing):
    context = transfer.prepare(api, '101', DELIVERY)
    api.transferred()
    api.logs[0]['logtype'] = 'h'
    if source_time_missing:
        api.rows[0].update(isremark=2, operatedate='', operatetime='')
    result = transfer.verify(api, '101', '合成意见\n金额 < 100', NOW, context)
    assert result['submit_ok'] and result['done_hit'] and result['postcheck_done_hit']


@pytest.mark.parametrize('source_time_missing', [False, True])
@pytest.mark.parametrize('change', ['old_recipient', 'old_log', 'wrong_person', 'wrong_node',
                                    'no_recipient', 'no_log', 'pending', 'old_time', 'external_user', 'source_pending',
                                    'recipient_missing_id'])
def test_transfer_readback_does_not_accept_partial_or_unrelated_evidence(api, change, source_time_missing):
    context = transfer.prepare(api, '101', DELIVERY)
    api.transferred()
    api.logs[0]['logtype'] = 'h'
    if source_time_missing:
        api.rows[0].update(isremark=2, operatedate='', operatetime='')
    if change == 'old_recipient':
        context['previous_operator_ids'].append('101')
    elif change == 'old_log':
        context['previous_log_ids'].append('201')
    elif change == 'wrong_person':
        api.logs[0]['receivedPersonids'] = '9'
    elif change == 'wrong_node':
        api.rows[-1]['nodeid'] = 30
    elif change == 'no_recipient':
        api.rows.pop()
    elif change == 'no_log':
        api.logs = []
    elif change == 'pending':
        api.pending = [{'request_id': '101'}]
    elif change == 'old_time':
        api.logs[0]['operatedate'] = '2000-01-01'
    elif change == 'external_user':
        api.rows[-1]['userType'] = 1
    elif change == 'recipient_missing_id':
        api.rows[-1].pop('id')
    else:
        api.rows[0]['isremark'] = 0
    assert transfer.verify(api, '101', '合成意见\n金额 < 100', NOW, context)['postcheck_done_hit'] is False


@pytest.mark.parametrize('source_fields,logtype', [
    ({'operatedate': '', 'operatetime': ''}, 'f'),
    ({'operatedate': '', 'operatetime': '12:00:00'}, 'h'),
    ({'operatedate': '2000-01-01', 'operatetime': '12:00:00'}, 'h'),
])
def test_transfer_readback_requires_transfer_log_for_blank_source_time(api, source_fields, logtype):
    context = transfer.prepare(api, '101', DELIVERY)
    api.transferred()
    api.rows[0].update(source_fields)
    api.logs[0]['logtype'] = logtype
    assert transfer.verify(api, '101', '合成意见\n金额 < 100', NOW, context)['postcheck_done_hit'] is False


@pytest.mark.parametrize('duplicate', ['recipient', 'log'])
def test_blank_source_time_requires_unambiguous_transfer_evidence(api, duplicate):
    context = transfer.prepare(api, '101', DELIVERY)
    api.transferred()
    api.rows[0].update(isremark=2, operatedate='', operatetime='')
    api.logs[0]['logtype'] = 'h'
    if duplicate == 'recipient':
        api.rows.append({**api.rows[-1], 'id': 102})
    else:
        api.logs.append({**api.logs[-1], 'id': 202})
    assert transfer.verify(api, '101', '合成意见\n金额 < 100', NOW, context)['postcheck_done_hit'] is False


@pytest.mark.parametrize('change', ['archive', 'no_edit', 'view_string', 'wrong_node', 'wrong_operator', 'self'])
def test_transfer_preflight_rejects_unwritable_or_changed_task(api, change):
    delivery = dict(DELIVERY)
    if change == 'archive':
        api.status['currentNodeType'] = '3'
    elif change == 'no_edit':
        api.workflow['canEdit'] = False
    elif change == 'view_string':
        api.workflow['canView'] = 'true'
    elif change == 'wrong_node':
        api.status['currentNodeId'] = 21
    elif change == 'wrong_operator':
        api.rows[0]['userid'] = 9
    else:
        delivery['recipient_id'] = '7'
    with pytest.raises(ValueError, match='transfer_(target|operator)_changed'):
        transfer.prepare(api, '101', delivery)


@pytest.mark.parametrize('response', ['SUCCESS', 'PARAM_ERROR', 'NO_PERMISSION', 'SYSTEM_INNER_ERROR', 'timeout'])
def test_writer_sends_only_one_transfer_with_opinion_and_never_submits(api, monkeypatch, response):
    context = transfer.prepare(api, '101', DELIVERY)
    posts = []
    def transport(request, **kwargs):
        assert urlparse(request.full_url).path == transfer.ENDPOINT
        posts.append(parse_qs(request.data.decode()))
        if response == 'timeout':
            raise TimeoutError()
        if response == 'SUCCESS':
            api.transferred()
        return BytesIO(json.dumps({'code': response}).encode())
    monkeypatch.setattr(http, 'urlopen', transport)
    client = writer.FrozenWriterApi('fake-token', 'fake-user', app_id='fake-app', base_url='https://oa.example.test')
    monkeypatch.setattr(client, 'get', api.get)
    monkeypatch.setattr(client, 'pending_rows', api.pending_rows)
    result = client.transfer_and_verify('101', '合成意见\n金额 < 100', context)
    assert len(posts) == 1
    assert posts[0] == {'requestId': ['101'], 'forwardFlag': ['3'], 'forwardResourceIds': ['8'],
                        'userId': ['7'], 'remark': ['合成意见<br/>金额 &lt; 100']}
    assert result['postcheck_done_hit'] is (response == 'SUCCESS')
    if response in {'PARAM_ERROR', 'NO_PERMISSION'}:
        assert result['failure_code'] == 'writer_submit_rejected'
    elif response in {'SYSTEM_INNER_ERROR', 'timeout'}:
        assert result['failure_code'] == 'writer_transport_unknown'


def test_log_pages_must_be_complete_and_not_repeat(api, monkeypatch):
    calls = []
    original = api.get
    def get(path, params=None):
        if path != transfer.LOGS_PATH:
            return original(path, params)
        calls.append(json.loads(params['otherParams']))
        return {'code': 'SUCCESS', 'data': [{'id': i} for i in range(1, 101)]}
    monkeypatch.setattr(api, 'get', get)
    with pytest.raises(ValueError, match='transfer_log_pagination_incomplete'):
        transfer.prepare(api, '101', DELIVERY)
    assert [p['pageNumber'] for p in calls] == [1, 2]


@pytest.mark.parametrize('state', [None, '', 'unexpected', '-1'])
def test_unknown_original_operator_state_cannot_confirm_transfer(api, state):
    context = transfer.prepare(api, '101', DELIVERY)
    api.transferred()
    api.rows[0]['isremark'] = state
    assert transfer.verify(api, '101', '合成意见\n金额 < 100', NOW, context)['done_hit'] is False


def test_recipient_already_pending_at_source_node_is_not_transferred_again(api):
    api.rows.append({'id': 102, 'userid': 8, 'userType': 0, 'nodeid': 20, 'isremark': 0})
    with pytest.raises(ValueError, match='transfer_recipient_already_pending'):
        transfer.prepare(api, '101', DELIVERY)
