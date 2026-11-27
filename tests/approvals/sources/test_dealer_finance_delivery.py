from types import SimpleNamespace

import pytest

from apps.oa.approvals.rules import freeze_rules
from apps.oa.approvals.sources import dealer_finance_delivery, oa, writer


@pytest.mark.parametrize(('flow_key', 'workflow_id', 'node'), [
    ('dealer_temporary_refund', '195056', '225119'),
    ('dealer_balance_adjustment', '194522', '225120'),
])
def test_dealer_finance_delivery_requires_the_exact_preaudit_node_and_freezes_rules(
        monkeypatch, tmp_path, flow_key, workflow_id, node):
    row = {'requestId': '901', 'workflowId': workflow_id, 'userId': '11',
           'cid': '912', 'nodeid': node, 'isremark': '0', 'viewtype': '0'}
    workflow = {'requestId': '901', 'workflowId': workflow_id, 'currentNodeId': node,
                'canEdit': True, 'submitButtonName': '提交'}
    actual = {'requestid': 901, 'workflowid': int(workflow_id), 'currentnodeid': int(node),
              'currentnodetype': '1', 'nodename': 'RPA预审'}
    api = SimpleNamespace(pending_rows=lambda: [oa._pending_case(row)])
    monkeypatch.setattr(dealer_finance_delivery.official_sql, '_query_rows',
                        lambda name, params, **kwargs: [actual])
    context = dealer_finance_delivery.snapshot(api, workflow, [], origins=None)
    assert context['delivery_kind'] == 'approval'
    assert context['task_instance_id'] == '912'
    assert freeze_rules(flow_key, tmp_path).is_file()
    row['nodeid'] = workflow['currentNodeId'] = '913'
    actual['currentnodeid'] = 913
    with pytest.raises(ValueError, match='dealer_finance_preaudit_node_mismatch'):
        dealer_finance_delivery.snapshot(api, workflow, [], origins=None)


def test_archived_forward_feedback_checks_same_delivery_and_unchanged_actual_node(monkeypatch):
    row = {'requestId': '901', 'workflowId': '195056', 'userId': '11', 'cid': '912',
           'nodeid': '913', 'isremark': '1', 'viewtype': '0', 'receiveTime': '2026-01-01 12:00:00'}
    workflow = {'requestId': '901', 'workflowId': '195056', 'currentNodeId': '913',
                'canEdit': True, 'submitButtonName': '批注'}
    actual = {'requestid': 901, 'workflowid': 195056, 'currentnodeid': 914,
              'currentnodetype': '3', 'nodename': '归档'}
    original = actual.copy()
    api = writer.FrozenWriterApi('token', 'user', base_url='https://example.invalid', app_id='fake')
    remaining = [oa._pending_case(row)]
    monkeypatch.setattr(api, 'pending_rows', lambda: remaining)
    posted = []

    def query(name, params, **kwargs):
        if name == 'dealer_finance_current_node':
            return [actual.copy()]
        if name == 'dealer_closure_forward_log':
            assert params['node_id'] == 913
            return [{'operator_id': 12, 'logid': 500}]
        assert name == 'dealer_closure_feedback_logs'
        assert params['node_id'] == 913 and params['operator_id'] == 11
        return [{'remark': '已核对', 'remark_length': 3}]

    def submit(request_id, remark):
        posted.append((request_id, remark))
        remaining[:] = [oa._pending_case({**row, 'cid': '999'})]
        return {'code': 'SUCCESS'}

    monkeypatch.setattr(dealer_finance_delivery.official_sql, '_query_rows', query)
    monkeypatch.setattr(api, '_post_submit', submit)
    context = dealer_finance_delivery.snapshot(api, workflow, [], origins=None)
    result = api.submit_and_verify('901', '已核对', dealer_finance_context=context)
    assert all(result[key] for key in ('submit_ok', 'done_hit', 'postcheck_done_hit'))
    assert posted == [('901', '已核对')]
    assert actual == original
    actual['currentnodeid'] = 915
    assert not dealer_finance_delivery.feedback_verified(context, '已核对', '2026-01-01T12:00:00+08:00')
    assert posted == [('901', '已核对')]
