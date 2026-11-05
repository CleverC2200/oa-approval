from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
import re
import time
import urllib.parse
import urllib.request

from apps.oa.approvals.source_policy import load_source_policy
from apps.oa.approvals.sources import auth, browser_session, http
from apps.oa.approvals.sources.travel_read_auth import ACCOUNT_PATH, verified_identity


PREDICTION_PATH = '/api/workflow/layout/getSimpleModeData'


class _PredictionSession:
    def __init__(self, deadline):
        policy = load_source_policy()
        self.origin = policy.attachments.oa_origin
        http._origin(self.origin)
        self.deadline = deadline
        host = urllib.parse.urlsplit(self.origin).hostname
        try:
            state = browser_session.capture_storage_state(
                policy.browser.cdp_endpoint, allowed_hosts={host}, required_host=host,
                timeout_sec=min(5, self.remaining()),
            )
        except browser_session.RpaSessionError as error:
            raise RuntimeError(error.code) from None
        self.cookie = '; '.join(f"{item['name']}={item['value']}" for item in state['cookies'])
        if not verified_identity(self.read(ACCOUNT_PATH), auth.env('OA_UID')):
            raise RuntimeError('prediction_session_identity_mismatch')

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('prediction_timeout')
        return remaining

    def read(self, path, params=None):
        if path not in {ACCOUNT_PATH, PREDICTION_PATH}:
            raise ValueError('prediction_endpoint_not_allowed')
        request = urllib.request.Request(
            self.origin + path,
            data=urllib.parse.urlencode(params).encode() if params is not None else None,
            headers={'Cookie': self.cookie, 'Content-Type': 'application/x-www-form-urlencoded'},
        )
        with http.urlopen(request, timeout=self.remaining()) as response:
            raw = response.read()
            if response.headers.get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            return json.loads(raw.decode('utf-8-sig'))


def predict(request_id, *, timeout=60, refresh=False):
    if not isinstance(request_id, str) or not re.fullmatch(r'[1-9][0-9]*', request_id):
        raise ValueError('prediction_request_id_invalid')
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
        raise ValueError('prediction_timeout_invalid')
    deadline = time.monotonic() + timeout
    data = None
    try:
        session = _PredictionSession(deadline)
        params = {'requestid': request_id}
        if refresh:
            params['resetForecast'] = 1
        while True:
            payload = session.read(PREDICTION_PATH, params)
            forecast = payload.get('forecastInfo') if isinstance(payload, dict) else None
            nodes = payload.get('nodeInfo') if isinstance(payload, dict) else None
            if (not isinstance(forecast, dict) or not isinstance(forecast.get('isEnd'), bool)
                    or not isinstance(nodes, list) or not nodes
                    or any(not isinstance(node, dict) for node in nodes)):
                raise RuntimeError('prediction_response_invalid')
            data = payload
            if forecast['isEnd']:
                break
            params.pop('resetForecast', None)
            time.sleep(min(1, session.remaining()))
    except TimeoutError:
        pass
    complete = data is not None and data['forecastInfo']['isEnd']
    return {
        'status': 'ok' if complete else 'timeout', 'request_id': request_id,
        'source': 'oa_native_prediction', 'session_source': 'rpa',
        'queried_at': datetime.now(timezone.utc).isoformat(),
        'complete': complete,
        'has_unknown_nodes': data is not None and any(
            str(node.get('nodeid')) in {'-1', '-2'} for node in data['nodeInfo']
        ),
        'data': data,
    }


def render_text(result):
    lines = [f"流程 {result['request_id']} · OA 智能预测 · {result['status']}"]
    data = result['data'] or {}
    forecast = data.get('forecastInfo', {})
    if forecast:
        lines.append(
            f"预测来源：{forecast.get('forecastUserName', '')} / {forecast.get('forecastNodeName', '')} / "
            f"{forecast.get('forecastDate', '')} {forecast.get('forecastTime', '')}"
        )
    for index, node in enumerate(data.get('nodeInfo', []), 1):
        node_id = str(node.get('nodeid', ''))
        name = node.get('nodeName') or '未命名节点'
        state = {0: '已经过', 1: '当前', 3: '预测'}.get(node.get('nodeType'), '未知状态')
        if node_id == '-1':
            name, state = '智能预测中', '未完成'
        elif node_id == '-2':
            name, state = '后续节点无法预知', '未知'
        operators = ' → '.join(
            part.split('_#WFSPSTR_OPTTP#_')[-1]
            for part in str(node.get('nodeOperatorName') or '').split('->')
        )
        branch = ''
        if (node.get('branchNumber') or 0) > 1:
            branch = f" · 分支 {node.get('currentBranch')}/{node['branchNumber']}"
        lines.append(f'{index}. [{state}] {name} ({node_id}) · {operators or "未返回办理人"}{branch}')
    if not result['complete']:
        lines.append('预测等待超时，以上仅为已返回的部分结果。')
    lines.append('预测路径可能随表单和人员变化；未提交审批。')
    return '\n'.join(lines)
