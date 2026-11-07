from __future__ import annotations

import json
import re

from apps.oa.runtime.environment import stable_env_file


WORKFLOWS = frozenset({'travel_expense', 'business_entertainment', 'business_support_fee',
                       'small_marketing_fee', 'general_expense'})
FINANCE_NODE_NAMES = frozenset({'财务审核', '财务收单', '分支总账/结算会计审批'})


def policy_path():
    return stable_env_file().with_name('prediction-finance-precheck.json')


def load_policy():
    try:
        data = json.loads(policy_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError('finance_precheck_configuration_invalid') from None
    if (not isinstance(data, dict) or not isinstance(data.get('leader'), dict)
            or not isinstance(data.get('initiators'), list) or not data['initiators']):
        raise ValueError('finance_precheck_configuration_invalid')
    for person in [data['leader'], *data['initiators']]:
        if (not isinstance(person, dict) or not isinstance(person.get('id'), str)
                or not re.fullmatch(r'[1-9][0-9]*', person['id'])
                or not isinstance(person.get('name'), str) or not person['name'].strip()):
            raise ValueError('finance_precheck_configuration_invalid')
    if len({person['id'] for person in data['initiators']}) != len(data['initiators']):
        raise ValueError('finance_precheck_configuration_invalid')
    return data


def unresolved_path(node, *, leader_id=None):
    if node['state'] not in {'unknown', 'loading'}:
        return False
    resolution = node.get('resolution', {})
    return not (node['state'] == 'unknown' and (
        resolution.get('kind') == 'terminal_self_loop'
        or (leader_id is not None and resolution.get('kind') == 'finance_tail_excludes_leader'
            and resolution.get('leader_id') == leader_id)))


def assess(result, policy):
    if policy is None:
        return {'status': 'not_configured', 'required': None, 'reasons': [], 'uncertainties': ['policy_missing']}
    reasons, uncertainties = [], set()
    initiator = result['initiator']
    if initiator['read_status'] != 'ok':
        uncertainties.add('initiator_unavailable')
    elif initiator['id'] in {person['id'] for person in policy['initiators']}:
        reasons.append({'code': 'initiator_in_list', 'initiator_id': initiator['id'],
                        'initiator_name': initiator['name']})
    if not result['complete']:
        uncertainties.add('prediction_incomplete')
    for node in result['nodes']:
        if unresolved_path(node, leader_id=policy['leader']['id']):
            uncertainties.add('prediction_path_unknown')
        if node['state'] != 'predicted':
            continue
        if node['node_kind'] in {'0', '3', '5', '6'}:
            continue
        if node['node_kind'] not in {'1', '2'}:
            uncertainties.add('node_kind_unknown')
            continue
        if not node['operators_loaded'] or not node['operators']:
            uncertainties.add('future_operators_unavailable')
            continue
        for operator in node['operators']:
            person_type = str(operator['type'])
            if person_type not in {'0', '1', '2'}:
                uncertainties.add('operator_type_unknown')
                continue
            if person_type != '0' or operator['id'] != policy['leader']['id']:
                continue
            reasons.append({'code': 'future_approver_match', 'sequence': node['sequence'],
                            'node_id': node['node_id'], 'node_name': node['node_name'],
                            'approver_id': operator['id'], 'approver_name': policy['leader']['name']})
    if reasons:
        required, status = True, 'required'
    elif uncertainties:
        required, status = None, 'unknown'
    else:
        required, status = False, 'not_required'
    return {'status': status, 'required': required, 'reasons': reasons, 'uncertainties': sorted(uncertainties)}


def delivery_plan(result, policy):
    assessment = result['finance_precheck']
    plan = {'action': 'blocked', 'request_id': result['request_id'],
            'finance_precheck': assessment, 'prediction_queried_at': result['queried_at']}
    if policy is None or assessment['required'] is False:
        return {**plan, 'action': 'submit'}
    if assessment['required'] is not True or not result['complete']:
        return {**plan, 'reason': 'finance_prediction_incomplete'}
    current = [node for node in result['nodes'] if node['state'] == 'current']
    if len(current) != 1 or current[0]['node_kind'] not in {'1', '2'}:
        return {**plan, 'reason': 'finance_current_node_unavailable'}
    source = current[0]
    future = [node for node in result['nodes'] if node['sequence'] > source['sequence']]
    if ((source['branch_count'] or 0) > 1
            or any(unresolved_path(node) or (node['branch_count'] or 0) > 1 for node in future)):
        return {**plan, 'reason': 'finance_route_ambiguous'}
    for node in future:
        if node['state'] != 'predicted' or node['node_name'] not in FINANCE_NODE_NAMES:
            continue
        people = node['operators']
        if (node['node_kind'] not in {'1', '2'} or not node['operators_loaded'] or len(people) != 1
                or str(people[0]['type']) != '0' or not re.fullmatch(r'[1-9][0-9]*', people[0]['id'])):
            return {**plan, 'reason': 'finance_recipient_ambiguous'}
        return {**plan, 'action': 'transfer', 'source_node_id': source['node_id'],
                'finance_node_id': node['node_id'], 'finance_node_name': node['node_name'],
                'finance_node_sequence': node['sequence'], 'recipient_id': people[0]['id'],
                'recipient_name': people[0]['name']}
    return {**plan, 'reason': 'finance_future_node_unavailable'}
