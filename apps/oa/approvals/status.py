from __future__ import annotations

from collections import Counter
import json

from apps.oa.approvals.receipts import assess_submission
from apps.oa.approvals.store import AGENT_CLAIM_LIMIT, INFLIGHT_STATUSES, MAX_INFLIGHT


def batch_status(store, run, tasks, owner):
    frozen = json.loads(run['summary_json']).get('batch')
    scoped = tasks if frozen is None else [store.get_task(task_id) for task_id in frozen['task_ids']]
    task_ids = {task['id'] for task in scoped}
    outside = [task for task in tasks if task['id'] not in task_ids]
    queued = [task for task in scoped if task['status'] == 'queued']
    inflight = sum(task['status'] in INFLIGHT_STATUSES for task in scoped)
    candidate = next((task for task in tasks if task['id'] in task_ids and task['status'] == 'queued'), None)
    agents = []
    for agent_id in sorted({task['agent_id'] for task in tasks if task['agent_id'] is not None}):
        claimed = [task for task in tasks if task['agent_id'] == agent_id]
        busy = [task['id'] for task in claimed if task['status'] in INFLIGHT_STATUSES]
        agents.append({'agent_id': agent_id, 'workflow_keys': sorted({task['workflow_key'] for task in claimed}),
                       'claimed_count': len(claimed), 'inflight_task_ids': busy, 'busy': bool(busy),
                       'claim_limit_reached': len(claimed) >= AGENT_CLAIM_LIMIT})
    occupied = sum(task['status'] in INFLIGHT_STATUSES for task in tasks)
    acceptance = submission_acceptance(store, run['id'], scoped)
    all_acceptance = submission_acceptance(store, run['id'], tasks) if outside else acceptance
    owner_matches = owner.get('reachable') is True and owner.get('run_id') == run['id']
    blockers = closeout_blockers(all_acceptance,
        collection_active=owner_matches and owner.get('collection') is not None)
    if owner_state_unconfirmed(owner):
        blockers.append('owner_state_unconfirmed')
    owner_released = (owner.get('reachable') is False
                      or owner.get('reachable') is True and owner.get('run_id') is not None
                      and owner['run_id'] != run['id'])
    complete = run['status'] == 'closed' and owner_released
    execution = json.loads(run['summary_json']).get('execution')
    alerts = status_alerts(run, owner, all_acceptance, blockers, dict(Counter(task['status'] for task in tasks)))
    if execution and execution.get('stop') and execution['stop']['kind'] in {'unexplained', 'execution_incomplete'}:
        alerts.append({'code': 'batch_stopped_early', 'severity': 'blocked'})
    if execution:
        execution = {**execution, 'archive_eligible': execution['archive_eligible'] and owner_released and not blockers}
    else:
        execution = {'status': 'in_progress' if run['status'] != 'closed' else 'legacy_unverified',
                     'archive_eligible': False}
    from apps.oa.approvals.execution import execution_headline

    execution['headline'] = (execution_headline(execution) if owner_released
                             else '收尾待验｜等待执行进程退出')
    return {
        'pending': json.loads(run['summary_json']).get('pending'),
        'execution': execution,
        'acceptance': acceptance,
        'closeout': {'drained': not blockers, 'blockers': blockers,
                     'batch_complete': not blockers and not queued,
                     'complete': complete and not blockers and not queued},
        'alerts': alerts,
        'agents': agents,
        'capacity': {'inflight': occupied, 'available': max(0, MAX_INFLIGHT - occupied), 'limit': MAX_INFLIGHT},
        'batch': {
            'mode': 'all_queue' if frozen is None else 'frozen',
            'size': None if frozen is None else frozen['size'],
            'task_ids': [task['id'] for task in scoped], 'total': len(scoped),
            'queued': len(queued), 'inflight': inflight, 'terminal': len(scoped) - len(queued) - inflight,
            'remaining': len(queued) + inflight, 'queue_exhausted': not queued and not inflight,
            'summary': dict(Counter(task['status'] for task in scoped)),
            'outside_count': len(outside), 'outside_summary': dict(Counter(task['status'] for task in outside)),
        },
        'next_candidate': {key: candidate[key] for key in
            ('id', 'request_id', 'workflow_key', 'received_at', 'status')} if candidate is not None else None,
    }


def closeout_blockers(acceptance, *, collection_active=False):
    facts = acceptance['tasks']
    blockers = []
    if collection_active or any(task['status'] in INFLIGHT_STATUSES for task in facts):
        blockers.append('tasks_inflight')
    if any(task['status'] == 'unknown' or any(not attempt['resolved'] for attempt in task['submissions'])
           for task in facts):
        blockers.append('submission_unresolved')
    if any(task['status'] == 'submitted' and not task['verified_attempt_count']
           and all(attempt['resolved'] for attempt in task['submissions']) for task in facts):
        blockers.append('submission_unverified')
    return blockers


def owner_state_unconfirmed(owner):
    return (owner.get('reachable') is None
            or owner.get('reachable') is True and owner.get('run_id') is None)


def status_alerts(run, owner, acceptance, blockers, summary):
    codes = [code for code in blockers if code not in {
        'tasks_inflight', 'submission_unresolved', 'owner_state_unconfirmed'}]
    owner_running = run['status'] == 'running' and owner.get('reachable') is True and owner.get('run_id') == run['id']
    owner_busy = run['status'] == 'running' and owner_state_unconfirmed(owner)
    if any(task['status'] == 'unknown' or any(not attempt['resolved'] and not (
            (owner_running or owner_busy) and task['status'] == 'submitting' and attempt['status'] == 'started')
            for attempt in task['submissions']) for task in acceptance['tasks']):
        codes.append('submission_unresolved')
    if run['status'] in {'running', 'blocked'}:
        if owner.get('reachable') is False:
            codes.append('owner_unreachable')
        elif owner.get('reachable') is True and owner.get('run_id') is not None and owner['run_id'] != run['id']:
            codes.append('owner_mismatch')
        if run['status'] == 'blocked':
            codes.append('run_blocked')
    elif 'tasks_inflight' in blockers:
        codes.append('run_closed_with_inflight')
    alerts = [{'code': code, 'severity': 'blocked'} for code in codes]
    if owner_state_unconfirmed(owner):
        alerts.append({'code': 'owner_unresponsive', 'severity': 'warning'})
    if summary.get('failed'):
        alerts.append({'code': 'technical_failures', 'severity': 'warning', 'count': summary['failed']})
    if json.loads(run['summary_json']).get('handoff', {}).get('status') == 'ready':
        alerts.append({'code': 'continuation_pending', 'severity': 'warning'})
    return alerts


def submission_acceptance(store, run_id, tasks):
    submissions = store.submissions(run_id)
    facts = []
    for task in tasks:
        attempts = []
        for submission in submissions:
            if submission['task_id'] != task['id']:
                continue
            evidence = assess_submission(submission, workflow_key=task['workflow_key'])
            receipt = evidence.receipt
            attempts.append({key: submission[key] for key in
                ('id', 'review_id', 'status', 'started_at', 'completed_at')} | {
                key: receipt.get(key) for key in
                    ('submit_ok', 'done_hit', 'postcheck_done_hit', 'fields_match', 'failure_code')} | {
                    'verified': evidence.verified, 'resolved': evidence.resolved})
        facts.append({
            'task_id': task['id'], 'request_id': task['request_id'], 'status': task['status'],
            'attempt_count': len(attempts), 'duplicate_attempt_count': max(0, len(attempts) - 1),
            'verified_attempt_count': sum(item['verified'] for item in attempts),
            'unknown_attempt_count': sum(item['status'] == 'unknown' for item in attempts),
            'unfinished_attempt_count': sum(item['status'] == 'started' or item['completed_at'] is None for item in attempts),
            'unverified_attempt_count': sum(not item['verified'] for item in attempts),
            'submissions': attempts,
        })
    verified_tasks = sum(item['verified_attempt_count'] > 0 for item in facts)
    totals = {key: sum(item[key] for item in facts) for key in (
        'attempt_count', 'duplicate_attempt_count', 'verified_attempt_count', 'unknown_attempt_count',
        'unfinished_attempt_count', 'unverified_attempt_count')}
    return {
        'task_count': len(facts), **totals,
        'tasks_with_verified_submission': verified_tasks,
        'tasks_without_verified_submission': len(facts) - verified_tasks,
        'all_verified': bool(facts) and verified_tasks == len(facts) and totals['unverified_attempt_count'] == 0,
        'tasks': facts,
    }
