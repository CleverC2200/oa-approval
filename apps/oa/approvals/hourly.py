from __future__ import annotations

import json
from pathlib import Path

from apps.oa.runtime import control


def dispatch_plan(run, stored, owner, blockers, unresolved):
    execution = stored.get('execution') or {}
    stop = execution.get('stop') or {}
    handoff = stored.get('handoff') or {}
    run_id = run['id'] if run is not None else None

    if control.stop_requested() or stop.get('kind') == 'user_stop':
        action, reason = 'stopped', 'user_stop'
    elif owner.get('reachable') is not False:
        action, reason = 'inspect_owner', 'owner_state_unconfirmed'
        if run_id is not None and owner.get('run_id') == run_id:
            reason = 'owner_reachable'
    elif unresolved:
        action, reason = 'postcheck', 'submission_unresolved'
    elif 'submission_unresolved' in blockers or 'submission_unverified' in blockers:
        action, reason = 'blocked', 'submission_evidence_requires_review'
    elif run is None:
        action, reason = 'start', 'no_runs'
    elif run['status'] != 'closed':
        action, reason = 'confirm_stopped', 'active_round_without_owner'
    elif handoff.get('status') == 'ready':
        action, reason = 'continue', 'continuation_pending'
    elif stored.get('closure'):
        action, reason = 'start', stored['closure']['kind']
    elif handoff.get('status') == 'blocked' and handoff.get('reason') not in {
            'continuation_unresolved_tasks', 'continuation_unverified_submission'}:
        action, reason = 'blocked', handoff.get('reason', 'continuation_blocked')
    elif (handoff.get('status') == 'blocked' or 'tasks_inflight' in blockers or execution.get('unfinished_count')
          or stop.get('kind') in {'unexplained', 'execution_incomplete', 'material_read_failure'}):
        action, reason = 'confirm_stopped', 'incomplete_closed_round'
    else:
        action, reason = 'start', 'closed_round'
    return {'action': action, 'reason': reason, 'run_id': run_id}


def prepare_start(store, continue_from, stopped_run_file):
    from apps.oa.approvals.status import closeout_blockers, submission_acceptance

    run = store.latest_run()
    stored = json.loads(run['summary_json']) if run is not None else {}
    blockers = []
    if run is not None:
        tasks = store.tasks(run['id'])
        blockers = closeout_blockers(submission_acceptance(store, run['id'], tasks))
    plan = dispatch_plan(run, stored, {'reachable': False}, blockers, store.unresolved_writes())
    action, reason = plan['action'], plan['reason']
    if action in {'postcheck', 'stopped', 'blocked'}:
        raise ValueError(reason)
    store.continuation_options(continue_from)
    if stopped_run_file is None:
        if action == 'confirm_stopped':
            raise ValueError('stopped_run_evidence_required')
        return None
    if run is None:
        raise ValueError('run_mismatch')
    evidence = stopped_run_evidence(store.path.parent, stopped_run_file, run['id'])
    if action != 'confirm_stopped':
        return None
    if evidence['basis'] == 'orphan_close_receipt' and run['status'] != 'closed':
        raise ValueError('stopped_run_evidence_invalid')
    return store.close(run['id'], closure={'kind': 'hourly_recovery', **evidence})


def stopped_run_evidence(root, path, run_id):
    if not path.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('stopped_run_evidence_invalid')
    try:
        record = json.loads(control.read_control_bytes(path, max_bytes=16_384) or b'null')
    except (OSError, ValueError) as error:
        raise ValueError('stopped_run_evidence_invalid') from error
    if (not isinstance(record, dict) or record.get('basis') not in {
            'owner_lost', 'execution_stalled', 'orphan_close_receipt'}
            or not isinstance(record.get('evidence'), str) or not record['evidence'].strip()):
        raise ValueError('stopped_run_evidence_invalid')
    if record.get('run_id') != run_id:
        raise ValueError('run_mismatch')
    return {'basis': record['basis'], 'evidence': record['evidence'].strip()}
