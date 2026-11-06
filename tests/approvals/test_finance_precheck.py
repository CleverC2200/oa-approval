from __future__ import annotations

import json

import pytest

from apps.oa.approvals import finance_precheck


def test_policy_is_private_to_the_auth_configuration_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(finance_precheck, 'stable_env_file', lambda: tmp_path / 'oa-auth.env')
    path = tmp_path / 'prediction-finance-precheck.json'
    assert finance_precheck.policy_path() == path
    assert finance_precheck.load_policy() is None
    policy = {'leader': {'id': '100', 'name': '测试高层'},
              'initiators': [{'id': '4', 'name': '名单内发起人'}]}
    path.write_text(json.dumps(policy), encoding='utf-8')
    assert finance_precheck.load_policy() == policy


@pytest.mark.parametrize('text', ['bad-json', '{}',
    '{"leader":{"id":"100","name":"测试"},"initiators":[{"id":"4","name":"甲"},{"id":"4","name":"乙"}]}',
    '{"leader":{"id":"0","name":"测试"},"initiators":[{"id":"4","name":"甲"}]}',
])
def test_invalid_policy_is_not_silently_used_as_an_empty_list(monkeypatch, tmp_path, text):
    path = tmp_path / 'prediction-finance-precheck.json'
    path.write_text(text, encoding='utf-8')
    monkeypatch.setattr(finance_precheck, 'policy_path', lambda: path)
    with pytest.raises(ValueError, match='^finance_precheck_configuration_invalid$'):
        finance_precheck.load_policy()


def forecast(required=True):
    return {'request_id': '101', 'queried_at': '2026-01-01T00:00:00+00:00', 'complete': True,
            'finance_precheck': {'required': required, 'status': 'required' if required else 'not_required'},
            'nodes': [
                {'sequence': 1, 'node_id': '10', 'node_name': '预审', 'node_kind': '1', 'state': 'current',
                 'branch_count': 1},
                {'sequence': 2, 'node_id': '20', 'node_name': '财务收单', 'node_kind': '1', 'state': 'predicted',
                 'branch_count': 1, 'operators_loaded': True,
                 'operators': [{'id': '8', 'name': '合成财务', 'type': 0}]},
                {'sequence': 3, 'node_id': '30', 'node_name': '财务审核', 'node_kind': '1', 'state': 'predicted',
                 'branch_count': 1, 'operators_loaded': True,
                 'operators': [{'id': '9', 'name': '另一财务', 'type': 0}]}]}


def test_delivery_uses_first_future_manual_finance_node_and_keeps_sequence():
    result = forecast()
    delivery = finance_precheck.delivery_plan(result, {})
    assert (delivery['action'], delivery['recipient_id'], delivery['finance_node_sequence']) == ('transfer', '8', 2)
    result['nodes'][1]['state'] = 'passed'
    delivery = finance_precheck.delivery_plan(result, {})
    assert (delivery['recipient_id'], delivery['finance_node_sequence']) == ('9', 3)
    result['nodes'][1]['state'] = 'predicted'
    result['nodes'][1]['node_name'] = '分支总账/结算会计审批'
    assert finance_precheck.delivery_plan(result, {})['recipient_id'] == '8'


@pytest.mark.parametrize('change', ['unknown', 'multiple', 'robot', 'unloaded', 'branch', 'current_branch', 'incomplete', 'archive', 'missing'])
def test_delivery_blocks_incomplete_path_or_ambiguous_first_recipient(change):
    result = forecast()
    first = result['nodes'][1]
    if change == 'unknown':
        first['state'] = 'unknown'
    elif change == 'multiple':
        first['operators'].append({'id': '9', 'name': '另一人', 'type': 0})
    elif change == 'robot':
        first['operators'][0]['type'] = 2
    elif change == 'unloaded':
        first['operators_loaded'] = False
    elif change == 'branch':
        first['branch_count'] = 2
    elif change == 'current_branch':
        result['nodes'][0]['branch_count'] = 2
    elif change == 'incomplete':
        result['complete'] = False
    elif change == 'archive':
        result['nodes'][0]['node_kind'] = '3'
    else:
        result['nodes'] = result['nodes'][:1]
    assert finance_precheck.delivery_plan(result, {})['action'] == 'blocked'


def test_delivery_does_not_replace_an_unknown_decision_with_normal_submission():
    assert finance_precheck.delivery_plan(forecast(None), {})['action'] == 'blocked'
    assert finance_precheck.delivery_plan(forecast(False), {})['action'] == 'submit'
    assert finance_precheck.delivery_plan(forecast(), None)['action'] == 'submit'
    result = forecast()
    result['nodes'][-1]['state'] = 'unknown'
    assert finance_precheck.delivery_plan(result, {})['action'] == 'blocked'
    result['nodes'][-1]['state'] = 'predicted'
    result['nodes'][-1]['branch_count'] = 2
    assert finance_precheck.delivery_plan(result, {})['action'] == 'blocked'
