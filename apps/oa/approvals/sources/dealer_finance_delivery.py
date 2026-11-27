from datetime import datetime
import html
import re
from zoneinfo import ZoneInfo

from apps.oa.approvals import attachment_exception, flows
from apps.oa.approvals.sources import official_sql


def actual_node(request_id, workflow_id):
    rows = official_sql._query_rows('dealer_finance_current_node', {
        'request_id': int(request_id), 'workflow_id': int(workflow_id),
    }, require_connection=True)
    if (len(rows) != 1 or str(rows[0]['requestid']) != request_id
            or str(rows[0]['workflowid']) != workflow_id or not rows[0]['currentnodeid']):
        raise ValueError('dealer_finance_actual_node_unverified')
    return rows[0]


def snapshot(api, workflow, resources, *, origins):
    request_id = str(workflow.get('requestId') or '')
    workflow_id = flows.workflow_identity(workflow)['workflow_id']
    if not request_id.isdecimal() or workflow_id not in flows.DEALER_FINANCE_NODES:
        raise ValueError('dealer_finance_workflow_mismatch')
    if workflow.get('canEdit') is not True:
        raise ValueError('dealer_finance_target_unavailable')
    rows = [row for row in api.pending_rows()
            if row['request_id'] == request_id and row['workflow_id'] == workflow_id]
    if len(rows) != 1:
        raise ValueError('dealer_finance_delivery_ambiguous')
    delivery = rows[0]
    operator_node = str(workflow.get('currentNodeId') or '')
    if (not all(str(delivery.get(key) or '').isdecimal()
                for key in ('task_instance_id', 'operator_id', 'operator_node_id'))
            or delivery['operator_node_id'] != operator_node):
        raise ValueError('dealer_finance_operator_unverified')
    actual = actual_node(request_id, workflow_id)
    action = delivery.get('delivery_kind')
    button = workflow.get('submitButtonName')
    forward_log = None
    if action == 'comment':
        if button != '批注' or delivery.get('view_type') != '0':
            raise ValueError('dealer_finance_feedback_context_invalid')
        received = datetime.fromisoformat(delivery['received_at'])
        forwarded = official_sql._query_rows('dealer_closure_forward_log', {
            'request_id': int(request_id), 'node_id': int(operator_node),
            'received_date': received.strftime('%Y-%m-%d'),
            'received_time': received.strftime('%H:%M:%S'),
        }, require_connection=True)
        if len(forwarded) != 1 or not str(forwarded[0].get('operator_id') or '').isdecimal():
            raise ValueError('dealer_finance_forwarder_unverified')
        forward_log = forwarded[0]
    elif action == 'approval':
        if (not isinstance(button, str) or not button or button == '批注'
                or str(actual['currentnodetype']) != '1'
                or str(actual['currentnodeid']) != operator_node
                or operator_node != flows.DEALER_FINANCE_NODES[workflow_id]
                or actual['nodename'] != 'RPA预审'):
            raise ValueError('dealer_finance_preaudit_node_mismatch')
    else:
        raise ValueError('dealer_finance_delivery_unknown')
    return {'request_id': request_id, 'workflow_id': workflow_id,
            'task_instance_id': delivery['task_instance_id'], 'operator_id': delivery['operator_id'],
            'delivery_kind': action, 'operator_node_id': operator_node, 'actual_node': actual,
            'forward_log': forward_log, 'submit_button_name': button,
            'form_digest': attachment_exception.context_digest(workflow, resources, origins=origins)}


def validate(api, expected, *, origins):
    workflow, resources = api.request_context(expected['request_id'])
    if snapshot(api, workflow, resources, origins=origins) != expected:
        raise ValueError('dealer_finance_submission_context_changed')


def pending_matches(rows, context):
    return any(row['request_id'] == context['request_id']
               and row.get('workflow_id') == context['workflow_id']
               and row.get('task_instance_id') == context['task_instance_id'] for row in rows)


def feedback_verified(context, opinion, started_at):
    request_id = context['request_id']
    if actual_node(request_id, context['workflow_id']) != context['actual_node']:
        return False
    started = datetime.fromisoformat(started_at).astimezone(ZoneInfo('Asia/Taipei'))
    entries = official_sql._query_rows('dealer_closure_feedback_logs', {
        'request_id': int(request_id), 'node_id': int(context['operator_node_id']),
        'operator_id': int(context['operator_id']), 'started_date': started.strftime('%Y-%m-%d'),
        'started_time': started.strftime('%H:%M:%S'),
    }, require_connection=True)

    def normalized(value):
        return ''.join(html.unescape(re.sub(r'<[^>]+>', '', value)).split())

    for entry in entries:
        remark = str(entry.get('remark') or '')
        if len(remark) == entry.get('remark_length') and normalized(remark) == normalized(opinion):
            return True
    return False
