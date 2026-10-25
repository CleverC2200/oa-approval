from __future__ import annotations

from collections import Counter
import json

from apps.oa.approvals.store import AGENT_CLAIM_LIMIT, INFLIGHT_STATUSES, MAX_INFLIGHT


def batch_status(store, run, tasks):
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
    return {
        'acceptance': submission_acceptance(store, run['id'], scoped),
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


def submission_acceptance(store, run_id, tasks):
    submissions = store.submissions(run_id)
    facts = []
    for task in tasks:
        attempts = []
        for submission in submissions:
            if submission['task_id'] != task['id']:
                continue
            receipt = json.loads(submission['result_json'])
            verified = (submission['status'] == 'submitted' and submission['completed_at'] is not None
                        and all(receipt.get(key) is True for key in ('submit_ok', 'done_hit', 'postcheck_done_hit'))
                        and receipt.get('fields_match') is not False)
            attempts.append({key: submission[key] for key in
                ('id', 'review_id', 'status', 'started_at', 'completed_at')} | {
                key: receipt.get(key) for key in
                ('submit_ok', 'done_hit', 'postcheck_done_hit', 'fields_match', 'failure_code')} | {'verified': verified})
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
