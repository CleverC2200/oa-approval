from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from apps.oa.approvals.sources import auth, http
from apps.oa.approvals.sources.travel_read_auth import ACCOUNT_PATH, verified_identity
from apps.oa.approvals import finance_precheck


PREDICTION_PATH = '/api/workflow/layout/getSimpleModeData'
OPERATOR_PATH = '/api/workflow/layout/getOperator'
REQUEST_PATH = '/api/workflow/paService/getWorkflowRequest'


class _PredictionSession:
    def __init__(self, timeout):
        self.origin = auth.env('OA_BASE_URL').rstrip('/')
        http._origin(self.origin)
        token, user_id = auth.credentials(base_url=self.origin)
        self.headers = {'appid': auth.env('OA_APP_ID'), 'token': token, 'userid': user_id,
                        'Content-Type': 'application/x-www-form-urlencoded'}
        self.deadline = time.monotonic() + timeout
        self.operator_cache = {}
        self.operator_responses = []
        if not verified_identity(self.read(ACCOUNT_PATH), auth.env('OA_UID')):
            raise RuntimeError('prediction_session_identity_mismatch')

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('prediction_timeout')
        return remaining

    def read(self, path, params=None):
        if path not in {ACCOUNT_PATH, PREDICTION_PATH, OPERATOR_PATH, REQUEST_PATH}:
            raise ValueError('prediction_endpoint_not_allowed')
        query = urllib.parse.urlencode(params or {})
        use_query = path == REQUEST_PATH
        request = urllib.request.Request(
            self.origin + path + ('?' + query if use_query else ''),
            data=query.encode() if params is not None and not use_query else None,
            headers=self.headers,
        )
        try:
            with http.urlopen(request, timeout=self.remaining()) as response:
                raw = response.read()
                if response.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode('utf-8-sig'))
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError('prediction_timeout') from None
            raise

    def initiator(self, request_id):
        payload = self.read(REQUEST_PATH, {'requestId': request_id})
        data = payload.get('data') if isinstance(payload, dict) and payload.get('code') == 'SUCCESS' else None
        if (not isinstance(data, dict) or str(data.get('requestId')) != request_id
                or data.get('canView') is not True
                or not re.fullmatch(r'[1-9][0-9]*', str(data.get('creatorId', '')))):
            raise ValueError('prediction_initiator_unavailable')
        return {'id': str(data['creatorId']), 'name': str(data.get('creatorName') or ''),
                'read_status': 'ok', 'source': REQUEST_PATH}

    def operators(self, request_id, node_id, node_type):
        key = (node_id, node_type)
        if key not in self.operator_cache:
            params = {'requestid': request_id, 'cnodeid': node_id, 'nodeType': node_type}
            if node_type == 3:
                params['isForecastNode'] = 1
            payload = self.read(OPERATOR_PATH, params)
            groups = payload.get('nodes') if isinstance(payload, dict) else None
            if (not isinstance(groups, list) or not groups
                    or any(not isinstance(group, dict) or not isinstance(group.get('operators'), list)
                           for group in groups)):
                raise RuntimeError('prediction_operators_invalid')
            operators = []
            for group in groups:
                for operator in group['operators']:
                    if (not isinstance(operator, dict)
                            or not re.fullmatch(r'0|[1-9][0-9]*', str(operator.get('id', '')))
                            or not isinstance(operator.get('name'), str) or not operator['name'].strip()):
                        raise RuntimeError('prediction_operators_invalid')
                    operators.append({'id': str(operator['id']), 'name': operator['name'],
                                      'type': operator.get('type')})
            self.operator_cache[key] = operators
            self.operator_responses.append({'request': params, 'response': payload})
        return self.operator_cache[key]


def _all_nodes(payload):
    nodes = []
    for sequence, raw in enumerate(payload['nodeInfo'], 1):
        node_id = str(raw.get('nodeid', ''))
        if not re.fullmatch(r'-?(0|[1-9][0-9]*)', node_id):
            raise RuntimeError('prediction_response_invalid')
        node_type = raw.get('nodeType')
        state = 'unknown'
        if type(node_type) is int:
            state = {0: 'passed', 1: 'current', 3: 'predicted'}.get(node_type, 'unknown')
        if node_id == '-1':
            state = 'loading'
        elif int(node_id) <= 0:
            state = 'unknown'
        nodes.append({
            'sequence': sequence, 'node_type': node_type,
            'node_kind': str(raw['ntype']) if raw.get('ntype') is not None else None,
            'node_id': node_id,
            'node_name': raw.get('nodeName') or ('后续节点无法预知' if state == 'unknown' else '未命名节点'),
            'state': state, 'operators': [], 'operators_loaded': False,
            'branch_count': raw.get('branchNumber'), 'selected_branch': raw.get('currentBranch'),
        })
    return nodes


def predict(request_id, *, timeout=60, refresh=False):
    if not isinstance(request_id, str) or not re.fullmatch(r'[1-9][0-9]*', request_id):
        raise ValueError('prediction_request_id_invalid')
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
        raise ValueError('prediction_timeout_invalid')
    policy = finance_precheck.load_policy()
    initiator = {'id': None, 'name': '', 'read_status': 'unavailable', 'source': REQUEST_PATH}
    data, nodes, operator_responses, complete = None, [], [], False
    try:
        session = _PredictionSession(timeout)
        operator_responses = session.operator_responses
        if policy is not None:
            try:
                initiator = session.initiator(request_id)
            except (OSError, ValueError):
                pass
        params = {'requestid': request_id}
        if refresh:
            params['resetForecast'] = 1
        while True:
            payload = session.read(PREDICTION_PATH, params)
            forecast = payload.get('forecastInfo') if isinstance(payload, dict) else None
            raw_nodes = payload.get('nodeInfo') if isinstance(payload, dict) else None
            if (not isinstance(forecast, dict) or not isinstance(forecast.get('isEnd'), bool)
                    or not isinstance(raw_nodes, list) or not raw_nodes
                    or any(not isinstance(node, dict) for node in raw_nodes)):
                raise RuntimeError('prediction_response_invalid')
            nodes = _all_nodes(payload)
            data = payload
            if forecast['isEnd']:
                break
            params.pop('resetForecast', None)
            time.sleep(min(1, session.remaining()))
        for node in nodes:
            if node['state'] in {'passed', 'current', 'predicted'}:
                node['operators'] = session.operators(request_id, node['node_id'], node['node_type'])
                node['operators_loaded'] = True
        complete = True
    except TimeoutError:
        pass
    result = {
        'status': 'ok' if complete else 'timeout', 'request_id': request_id,
        'source': 'oa_native_prediction', 'session_source': 'api',
        'queried_at': datetime.now(timezone.utc).isoformat(),
        'complete': complete,
        'has_unknown_nodes': any(node['state'] in {'loading', 'unknown'} for node in nodes),
        'forecast': data['forecastInfo'] if data is not None else None,
        'nodes': nodes,
        'raw_prediction': data,
        'raw_operators': operator_responses,
        'initiator': initiator,
    }
    result['finance_precheck'] = finance_precheck.assess(result, policy)
    result['delivery'] = finance_precheck.delivery_plan(result, policy)
    return result


def render_text(result):
    lines = [f"流程 {result['request_id']} · 完整节点及流转预测 · {result['status']}"]
    forecast = result['forecast']
    if forecast:
        lines.append(
            f"预测来源：{forecast.get('forecastUserName', '')} / {forecast.get('forecastNodeName', '')} / "
            f"{forecast.get('forecastDate', '')} {forecast.get('forecastTime', '')}"
        )
    for index, node in enumerate(result['nodes'], 1):
        state = {'passed': '已经过', 'current': '当前', 'predicted': '预测',
                 'unknown': '未知', 'loading': '计算中'}[node['state']]
        operators = '、'.join(f"{operator['name']} ({operator['id']})" for operator in node['operators'])
        if node['state'] == 'unknown':
            operators = '无法预知办理人'
        elif not node['operators_loaded']:
            operators = '尚未取得办理人'
        branch = ''
        if (node['branch_count'] or 0) > 1:
            branch = f" · 分支 {node['selected_branch']}/{node['branch_count']}"
        lines.append(f"{index}. [{state}] {node['node_name']} ({node['node_id']}) · {operators or '无办理人'}{branch}")
    if not result['complete']:
        lines.append('查询等待超时，以上仅为已取得的部分结果。')
    assessment = result['finance_precheck']
    label = {'required': '需要财务前置', 'not_required': '本次预测与发起人未命中财务前置条件',
             'unknown': '财务前置判断不完整，不能确认无需财务前置', 'not_configured': '未配置财务前置规则'}
    lines.append('财务前置：' + label[assessment['status']])
    for reason in assessment['reasons']:
        if reason['code'] == 'initiator_in_list':
            lines.append(f"命中原因：发起人 {reason['initiator_name']} ({reason['initiator_id']}) 在指定名单中。")
        else:
            lines.append(f"命中原因：第 {reason['sequence']} 项 {reason['node_name']} 的预测办理人包含 "
                         f"{reason['approver_name']} ({reason['approver_id']})。")
    if assessment['uncertainties']:
        lines.append('判断缺口：' + '、'.join(assessment['uncertainties']))
    delivery = result['delivery']
    if delivery['action'] == 'transfer':
        lines.append(f"建议审核后转办：{delivery['recipient_name']} ({delivery['recipient_id']})，"
                     f"来源为第 {delivery['finance_node_sequence']} 项 {delivery['finance_node_name']}。")
    elif delivery['action'] == 'blocked':
        lines.append('处理动作待确认：' + delivery['reason'])
    lines.append('预测路径可能随表单和人员变化；未提交审批。')
    return '\n'.join(lines)
