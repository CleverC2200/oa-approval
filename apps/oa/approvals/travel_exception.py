import json

from apps.oa.runtime.environment import stable_env_file


SSO_REASONS = frozenset({'travel_sso_account_missing', 'travel_sso_company_missing'})
EXPLICIT_REASONS = SSO_REASONS | {'login_required'}


def validate(value, request_id):
    if (not isinstance(value, dict) or value.get('request_id') != str(request_id)
            or not isinstance(value.get('authorization_note'), str)
            or not value['authorization_note'].strip()):
        raise ValueError('travel_read_exception_invalid')
    reasons = value.get('allowed_reasons', sorted(EXPLICIT_REASONS))
    if (not isinstance(reasons, list) or not reasons
            or any(not isinstance(reason, str) or reason not in EXPLICIT_REASONS for reason in reasons)):
        raise ValueError('travel_read_exception_invalid')
    return value


def for_task(task, explicit=None):
    if explicit is not None:
        if task['workflow_key'] != 'travel_expense':
            raise ValueError('travel_read_exception_invalid')
        return validate(explicit, task['request_id'])
    if task['workflow_key'] != 'travel_expense':
        return None
    path = stable_env_file().with_name('travel-sso-authorization.json')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError('travel_sso_authorization_invalid') from None
    if not isinstance(value, dict):
        raise ValueError('travel_sso_authorization_invalid')
    reasons = value.get('allowed_reasons')
    if (not isinstance(reasons, list) or not reasons
            or any(not isinstance(reason, str) or reason not in SSO_REASONS for reason in reasons)):
        raise ValueError('travel_sso_authorization_invalid')
    bound = {'request_id': task['request_id'], 'authorization_note': value.get('authorization_note'),
             'allowed_reasons': reasons, 'authorization_source': path.name}
    try:
        return validate(bound, task['request_id'])
    except ValueError:
        raise ValueError('travel_sso_authorization_invalid') from None
