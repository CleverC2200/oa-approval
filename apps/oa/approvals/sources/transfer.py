from __future__ import annotations

from datetime import datetime
import html
import json
import re
from zoneinfo import ZoneInfo

from apps.oa.approvals import receipts
from apps.oa.approvals.sources import auth
from apps.oa.approvals.sources.travel_read_auth import ACCOUNT_PATH, verified_identity


ENDPOINT = '/api/workflow/paService/forwardRequest'
REQUEST_PATH = '/api/workflow/paService/getWorkflowRequest'
OPERATORS_PATH = '/api/workflow/paService/getRequestOperatorInfo'
LOGS_PATH = '/api/workflow/paService/getRequestLog'


def _data(api, path, params, kind):
    response = api.get(path, params)
    value = response.get('data')
    if response.get('code') != 'SUCCESS' or not isinstance(value, kind):
        raise ValueError('transfer_read_failed')
    if kind is list and any(not isinstance(row, dict) for row in value):
        raise ValueError('transfer_read_failed')
    return value


def _operators(api, request_id):
    return _data(api, OPERATORS_PATH, {'requestid': request_id}, list)


def _logs(api, request_id):
    rows, seen = [], set()
    for page in range(1, 101):
        batch = _data(api, LOGS_PATH, {'requestId': request_id,
            'otherParams': json.dumps({'pageSize': 100, 'pageNumber': page})}, list)
        for row in batch:
            key = str(row.get('id') or '')
            if not key or key in seen:
                raise ValueError('transfer_log_pagination_incomplete')
            seen.add(key)
            rows.append(row)
        if len(batch) < 100:
            return rows
    raise ValueError('transfer_log_pagination_incomplete')


def prepare(api, request_id, delivery):
    operator_id = auth.env('OA_UID')
    if not verified_identity(api.get(ACCOUNT_PATH), operator_id):
        raise ValueError('transfer_identity_mismatch')
    workflow = _data(api, REQUEST_PATH, {'requestId': request_id}, dict)
    status = _data(api, '/api/workflow/paService/getRequestStatus', {'requestId': request_id}, dict)
    if (str(workflow.get('requestId')) != request_id or workflow.get('canView') is not True
            or workflow.get('canEdit') is not True or str(status.get('requestId')) != request_id
            or str(status.get('currentNodeType')) == '3'
            or str(status.get('isremark')) != '0'
            or str(status.get('currentNodeId')) != delivery['source_node_id']
            or str(workflow.get('currentNodeId')) != delivery['source_node_id']
            or delivery['recipient_id'] == operator_id):
        raise ValueError('transfer_target_changed')
    rows = _operators(api, request_id)
    source = [row for row in rows if str(row.get('id')) == str(status.get('currentOperateId'))
              and str(row.get('userid')) == operator_id and str(row.get('userType')) == '0'
              and str(row.get('nodeid')) == delivery['source_node_id'] and str(row.get('isremark')) == '0']
    if len(source) != 1:
        raise ValueError('transfer_operator_changed')
    if any(str(row.get('userid')) == delivery['recipient_id'] and str(row.get('userType')) == '0'
            and str(row.get('nodeid')) == delivery['source_node_id'] and str(row.get('isremark')) == '0'
            for row in rows):
        raise ValueError('transfer_recipient_already_pending')
    return {**delivery, 'operator_id': operator_id, 'source_operator_id': str(source[0]['id']),
            'previous_operator_ids': [str(row['id']) for row in rows],
            'previous_log_ids': [str(row['id']) for row in _logs(api, request_id)]}


def _since(date, time, started_at):
    try:
        actual = datetime.fromisoformat(f'{date} {time}').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        started = datetime.fromisoformat(started_at).replace(microsecond=0)
        return actual >= started
    except (ValueError, TypeError):
        return False


def _text(value):
    value = re.sub(r'<br\s*/?>', '\n', str(value), flags=re.I)
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', '', value)).split())


def verify(api, request_id, opinion, started_at, delivery, *, submit_ok=None):
    rows = _operators(api, request_id)
    source_done = any(str(row.get('id')) == delivery['source_operator_id']
        and str(row.get('userid')) == delivery['operator_id'] and str(row.get('userType')) == '0'
        and str(row.get('nodeid')) == delivery['source_node_id'] and str(row.get('isremark')) in {'2', 'h'}
        and _since(row.get('operatedate'), row.get('operatetime'), started_at) for row in rows)
    recipient = any(str(row.get('id')) not in delivery['previous_operator_ids']
        and str(row.get('userid')) == delivery['recipient_id'] and str(row.get('userType')) == '0'
        and str(row.get('nodeid')) == delivery['source_node_id'] and str(row.get('isremark')) == '0'
        and _since(row.get('receivedate'), row.get('receivetime'), started_at) for row in rows)
    logged = any(str(row.get('id')) not in delivery['previous_log_ids']
        and str(row.get('operator')) == delivery['operator_id'] and str(row.get('operatortype')) == '0'
        and str(row.get('nodeid')) == delivery['source_node_id']
        and str(row.get('receivedPersonids') or '').strip() == delivery['recipient_id']
        and _since(row.get('operatedate'), row.get('operatetime'), started_at)
        and _text(row.get('remarkHtml')) == _text(html.escape(opinion))
        for row in _logs(api, request_id))
    pending = any(str(row.get('request_id')) == request_id for row in api.pending_rows())
    result = receipts.verification_result(source_done and recipient and logged, pending, submit_ok=submit_ok)
    return {**result, 'delivery': delivery,
            'failure_code': None if result['postcheck_done_hit'] else receipts.POSTCHECK_TIMEOUT}
