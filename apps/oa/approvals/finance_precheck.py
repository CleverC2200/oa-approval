from __future__ import annotations

import json
import re

from apps.oa.runtime.environment import stable_env_file


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
        if node['state'] in {'unknown', 'loading'}:
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
