from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import html
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request
from zoneinfo import ZoneInfo

from apps.oa.approvals.sources import official_sql
from apps.oa.runtime.environment import stable_env_file


def settings():
    path = stable_env_file().with_name('dealer-closure.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError('dealer_closure_configuration_unavailable')
    try:
        value = json.loads(path.read_text())
        required = ('doris_python', 'doris_connector', 'sap_python', 'sap_sdk',
                    'sap_host', 'sap_system', 'sap_sysnr', 'sap_client', 'sap_user',
                    'sap_keychain_service', 'dms_origin')
        if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k] for k in required):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise RuntimeError('dealer_closure_configuration_invalid') from None


def snapshot_context(api, workflow):
    request_id = str(workflow.get('requestId') or '')
    rows = [r for r in api.pending_rows() if r['request_id'] == request_id]
    if len(rows) != 1 or workflow.get('canEdit') is not True:
        raise RuntimeError('dealer_closure_target_unavailable')
    delivery = rows[0]
    if str(workflow.get('currentNodeId') or '') != delivery.get('operator_node_id'):
        raise RuntimeError('dealer_closure_operator_node_changed')
    action = delivery.get('delivery_kind')
    button = workflow.get('submitButtonName')
    if action == 'comment':
        if button != '批注' or delivery.get('view_type') != '0':
            raise RuntimeError('dealer_closure_feedback_context_invalid')
        received = datetime.fromisoformat(delivery['received_at'])
        forwarded = official_sql._query_rows('dealer_closure_forward_log',
            {'request_id': int(request_id), 'node_id': int(workflow['currentNodeId']),
             'received_date': received.strftime('%Y-%m-%d'), 'received_time': received.strftime('%H:%M:%S')},
            require_connection=True)
        if len(forwarded) != 1 or not re.fullmatch(r'[1-9][0-9]*', str(forwarded[0].get('operator_id', ''))):
            raise RuntimeError('dealer_closure_forwarder_unavailable')
    elif action == 'approval':
        if not isinstance(button, str) or not button or button == '批注':
            raise RuntimeError('dealer_closure_approval_context_invalid')
    else:
        raise RuntimeError('dealer_closure_delivery_unknown')
    actual = official_sql._query_rows('dealer_closure_current_node',
        {'request_id': int(request_id)}, require_connection=True)
    if (len(actual) != 1 or str(actual[0]['requestid']) != request_id
            or (action == 'approval' and (str(actual[0]['currentnodetype']) == '3'
                or str(actual[0]['currentnodeid']) != str(workflow.get('currentNodeId'))))):
        raise RuntimeError('dealer_closure_actual_node_invalid')
    return {'request_id': request_id, 'task_instance_id': delivery['task_instance_id'],
            'operator_id': delivery['operator_id'], 'delivery_kind': action,
            'forwarder_id': str(forwarded[0]['operator_id']) if action == 'comment' else None,
            'operator_node_id': str(workflow.get('currentNodeId') or ''),
            'submit_button_name': button, 'actual_node': actual[0]}


def _form_snapshot(workflow, resources):
    fields = {f['fieldName']: f.get('fieldValue') for row in workflow['workflowMainTableInfo']['requestRecords']
              for f in row['workflowRequestTableFields']}
    return {'fields': {key: fields.get(key) for key in ['lcbh', 'gsdm', 'jxsdm', 'jxsmc', 'yetk', 'sftk', 'je', 'fjsc1', 'fjsc2', 'tksqs']},
            'resources': sorted([[str(r.get('id') or ''), str(r.get('name') or '')] for r in resources])}


def _worker(mode, payload, config):
    from apps.oa.approvals.collection.case import CollectionFailure

    executable = config['sap_python' if mode == 'sap' else 'doris_python']
    command = [executable, str(Path(__file__).with_name('dealer_closure_reader.py')), mode]
    inputs = {**payload, 'connection': config}
    if mode == 'sap':
        secret = official_sql._keychain_password(config['sap_user'], config['sap_keychain_service'])
        if not secret:
            raise RuntimeError('dealer_closure_sap_credential_unavailable')
        inputs['sap_password'] = secret
    env = dict(os.environ)
    env['SAPNWRFC_HOME'] = config['sap_sdk']
    env['DYLD_LIBRARY_PATH'] = str(Path(config['sap_sdk']) / 'lib')
    try:
        result = subprocess.run(command, input=json.dumps(inputs), text=True,
            capture_output=True, timeout=300, env=env)
        data = json.loads(result.stdout)
        if result.returncode or data.get('status') != 'ok':
            raise CollectionFailure('dealer_closure_' + mode + '_read_failed', diagnostics=[
                {'stage': mode + '_' + str(data.get('stage', 'source')),
                 'reason': data.get('reason'), 'exception_type': data.get('error_type'),
                 'result_type': data.get('error_key')}])
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise CollectionFailure('dealer_closure_' + mode + '_read_failed', diagnostics=[
            {'stage': mode, 'reason': 'worker_response_unavailable'}]) from None


def _dms(customer, tag, config):
    body = {'interfaceTag': tag, 'reqJson': json.dumps({'customerCode': customer}),
            'reqTime': int(datetime.now().timestamp() * 1000)}
    request = urllib.request.Request(config['dms_origin'].rstrip('/') + '/api/v1/json/shareApi',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
            if response.status != 200 or result.get('success') is not True:
                raise ValueError()
        records = result.get('result')
        if records is not None:
            records = records if isinstance(records, list) else [records]
            if any(not isinstance(r, dict) or str(r.get('customerCode')) != customer for r in records):
                raise ValueError()
        return {'request': body, 'response': result}
    except (OSError, ValueError):
        raise RuntimeError('dealer_closure_dms_read_failed') from None


def _branch_context(fields, context):
    invalid = [code for code in ('yetk', 'sftk') if fields.get(code) not in {'0', '1'}]
    missing = [code for code in invalid if fields.get(code) in (None, '')]
    node_fields, deferred = [], []
    actual = context['actual_node']
    if missing == invalid and missing and actual.get('nodename') == 'AI预审' and str(actual.get('currentnodetype')) == '1':
        node_fields = official_sql._query_rows('dealer_closure_branch_fields',
            {'request_id': int(context['request_id'])}, require_connection=True)
        if (len(node_fields) != 2 or {r.get('fieldname') for r in node_fields} != {'yetk', 'sftk'}
                or any(str(r.get('currentnodeid')) != str(actual['currentnodeid'])
                    or r.get('isedit') not in {None, '0', '1'}
                    or r.get('ismandatory') not in {None, '0', '1'} for r in node_fields)):
            raise RuntimeError('dealer_closure_branch_stage_invalid')
        deferred = [code for code in missing if any(r['fieldname'] == code
            and r['isedit'] != '1' and r['ismandatory'] != '1' for r in node_fields)]
    remaining = [code for code in invalid if code not in deferred]
    if remaining:
        from apps.oa.approvals.collection.case import CollectionFailure
        raise CollectionFailure('dealer_closure_branch_invalid', diagnostics=[
            {'stage': 'dealer_closure_branch', 'source_path': code,
             'reason': 'missing_value' if code in missing else 'invalid_value'}
            for code in remaining])
    return {'deferred_fields': deferred, 'node_fields': node_fields}


def read(api, workflow, resources):
    context = snapshot_context(api, workflow)
    history = official_sql._query_rows('dealer_closure_history',
        {'request_id': int(context['request_id'])}, require_connection=True)
    if len(history) >= 1001 or any(len(str(r.get('remark') or '')) != (r.get('remark_length') or 0) for r in history):
        from apps.oa.approvals.collection.case import CollectionFailure
        raise CollectionFailure('dealer_closure_history_incomplete', diagnostics=[
            {'stage': 'oa_history', 'reason': 'row_limit' if len(history) >= 1001 else 'text_truncated'}])
    fields = {f['fieldName']: f.get('fieldValue') for row in workflow['workflowMainTableInfo']['requestRecords']
              for f in row['workflowRequestTableFields']}
    company, customer = str(fields.get('gsdm') or ''), str(fields.get('jxsdm') or '')
    if not re.fullmatch(r'[A-Za-z0-9]{4}', company) or not re.fullmatch(r'[0-9]{1,10}', customer):
        raise RuntimeError('dealer_closure_customer_identity_invalid')
    branch_context = _branch_context(fields, context)
    config = settings()
    now = datetime.now(ZoneInfo('Asia/Taipei'))
    payload = {'company_code': company, 'customer_code': customer, 'as_of': now.strftime('%Y%m%d')}
    bi = _worker('doris', payload, config)
    if not bi['rows'] or any(str(r.get('company_code')) != company or str(r.get('cust_code')) != customer
                            for r in bi['rows']):
        raise RuntimeError('dealer_closure_bi_identity_invalid')
    preaccrual = [r for r in bi['rows'] if r.get('ledger_account_control') and r.get('pre_end_balance') is not None]
    payload['preaccrual_accounts'] = (sorted({r['ledger_account_control'] for r in preaccrual})
        if any(Decimal(str(r['pre_end_balance'])) != 0 for r in preaccrual) else [])
    sap = _worker('sap', payload, config)
    if sap.get('customer_code') != customer or sap.get('company_code') != company:
        raise RuntimeError('dealer_closure_sap_identity_invalid')
    tags = ['querySignedOneAddNContractByCustomerCode']
    if branch_context['deferred_fields'] or (fields.get('yetk') == '0' and fields.get('sftk') == '0'):
        tags += [
        'queryLatestSignedBillByCustomerCode', 'queryLatestRefundApplyByCustomerCode',
        'queryPaymentAccountReportByCustomerCode']
    confirmation = [r for r in config.get('freezer_none_confirmations', [])
        if r.get('request_id') == context['request_id'] and r.get('task_instance_id') == context['task_instance_id']
        and r.get('company_code') == company and r.get('customer_code') == customer]
    return {'observed_at': now.isoformat(), 'company_code': company, 'customer_code': customer,
            'form_snapshot': _form_snapshot(workflow, resources),
            'submission_context': context, 'branch_context': branch_context, 'bi': bi, 'sap': sap,
            'history': {'source': 'official_sql.dealer_closure_history', 'complete': True, 'rows': history},
            'dms': {tag: _dms(customer, tag, config) for tag in tags},
            'resource_inventory': {'source': 'getRequestResources', 'request_id': context['request_id'],
                                   'resource_count': len(resources)},
            'freezer_none_confirmation': confirmation[0] if len(confirmation) == 1 else None}


def validate_submission(api, material):
    expected = material['dealer_closure_evidence']['submission_context']
    workflow, resources = api.request_context(material['request_id'])
    if _form_snapshot(workflow, resources) != material['dealer_closure_evidence']['form_snapshot']:
        raise ValueError('dealer_closure_form_changed')
    context = snapshot_context(api, workflow)
    if context != expected:
        raise ValueError('dealer_closure_submission_context_changed')
    if material['dealer_closure_evidence'].get('branch_context', {}).get('deferred_fields'):
        fields = _form_snapshot(workflow, resources)['fields']
        if _branch_context(fields, context) != material['dealer_closure_evidence']['branch_context']:
            raise ValueError('dealer_closure_branch_stage_changed')


def feedback_verified(api, material, opinion, started_at):
    context = material['dealer_closure_evidence']['submission_context']
    actual = official_sql._query_rows('dealer_closure_current_node',
        {'request_id': int(material['request_id'])}, require_connection=True)
    if actual != [context['actual_node']]:
        return False
    start = datetime.fromisoformat(started_at).astimezone(ZoneInfo('Asia/Taipei')).replace(microsecond=0)

    def text(value):
        return ''.join(html.unescape(re.sub(r'<[^>]+>', '', value)).split())

    entries = official_sql._query_rows('dealer_closure_feedback_logs',
        {'request_id': int(material['request_id']), 'node_id': int(context['operator_node_id']),
         'operator_id': int(context['operator_id']), 'started_date': start.strftime('%Y-%m-%d'),
         'started_time': start.strftime('%H:%M:%S')}, require_connection=True)
    for entry in entries:
        remark = str(entry.get('remark') or '')
        if len(remark) == entry.get('remark_length') and text(remark) == text(opinion):
            return True
    return False
