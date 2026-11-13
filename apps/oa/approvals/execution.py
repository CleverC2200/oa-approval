from pathlib import Path
import hashlib

from apps.oa.approvals.status import closeout_blockers, submission_acceptance


def validate_stop(store, run_id, value, *, handoff=False):
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get('kind') not in {
            'context_limit', 'material_read_failure', 'user_stop', 'execution_incomplete'}
            or not isinstance(value.get('reason'), str) or not value['reason'].strip()):
        raise ValueError('stop_reason_invalid')
    stop = {'kind': value['kind'], 'reason': value['reason']}
    if value['kind'] == 'user_stop' and handoff:
        raise ValueError('stop_handoff_not_allowed')
    if value['kind'] == 'material_read_failure':
        task_id = value.get('task_id')
        if type(task_id) is not int:
            raise ValueError('stop_read_failure_not_recorded')
        task = store.get_task(task_id)
        if task['last_run_id'] != run_id or task['status'] != 'failed' or not task['error']:
            raise ValueError('stop_read_failure_not_recorded')
        stop.update(task_id=task_id, error=task['error'])
    if value['kind'] in {'context_limit', 'execution_incomplete'}:
        path = Path(value.get('evidence_file', ''))
        root = store.path.parent.resolve()
        if not path.is_absolute() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError('stop_evidence_required')
        stop['evidence_file'] = str(path)
        if value['kind'] == 'context_limit':
            percent = value.get('context_used_percent')
            if type(percent) not in {int, float} or not 60 <= percent <= 100:
                raise ValueError('stop_context_evidence_invalid')
            stop['context_used_percent'] = percent
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        copy = root / 'execution-evidence' / run_id / f'{digest}.bin'
        copy.parent.mkdir(parents=True, exist_ok=True)
        if not copy.exists():
            copy.write_bytes(raw)
        elif copy.read_bytes() != raw:
            raise ValueError('stop_evidence_changed')
        stop.update(evidence_file=str(copy), evidence_sha256=digest)
    return stop


def close_execution(store, run, tasks, stop):
    import json

    frozen = json.loads(run['summary_json']).get('batch')
    scoped = tasks if frozen is None else [store.get_task(i) for i in frozen['task_ids']]
    acceptance = submission_acceptance(store, run['id'], scoped)
    queued = sum(t['status'] == 'queued' for t in scoped)
    failed = sum(t['status'] == 'failed' or any(
        a['run_id'] == run['id'] and a['assessment']['status'] == 'technical_failure'
        for a in store.review_assessments(t['id'])) for t in scoped)
    review_incomplete = any(a['run_id'] == run['id'] and a['assessment']['status'] == 'incomplete'
                            for t in scoped if t['status'] != 'failed' for a in store.review_assessments(t['id']))
    preview = sum(t['status'] == 'preview' for t in scoped)
    verified = sum(t['verified_attempt_count'] > 0 for t in acceptance['tasks'])
    unfinished = sum(t['status'] in {'preparing', 'reviewing', 'reviewed'} for t in scoped)
    unverified = sum(t['status'] == 'submitted' and not t['verified_attempt_count'] for t in acceptance['tasks'])
    if queued and stop is None:
        stop = {'kind': 'unexplained', 'reason': 'batch_closed_before_queue_drained'}
    if closeout_blockers(acceptance) or review_incomplete:
        status = 'blocked'
    elif failed:
        status = 'partial_failure' if verified else 'failed'
    elif stop and stop['kind'] in {'unexplained', 'execution_incomplete', 'material_read_failure'}:
        status = 'blocked'
    elif queued:
        status = 'continued' if stop['kind'] == 'context_limit' else 'stopped'
    elif preview:
        status = 'preview'
    else:
        status = 'success'
    return {'status': status, 'verified_count': verified, 'failure_count': failed,
            'preview_count': preview, 'unclaimed_count': queued,
            'unfinished_count': unfinished, 'unverified_count': unverified,
            'unknown_count': acceptance['unknown_attempt_count'] + acceptance['unfinished_attempt_count'], 'stop': stop,
            'archive_eligible': status == 'success' and not queued and not preview}


def execution_headline(execution):
    state = execution['status']
    verified = execution.get('verified_count', 0)
    failed = execution.get('failure_count', 0)
    if state == 'success':
        return f'✅ 成功｜已提交并验证 {verified} 单' if verified else '✅ 成功｜无待审单据'
    if state in {'partial_failure', 'failed'}:
        label = '部分失败' if verified else '失败'
        return f'❌ {label}｜成功 {verified} 单，失败 {failed} 单'
    if state == 'blocked':
        pending = execution['unclaimed_count'] + execution['unfinished_count'] + execution['unverified_count']
        return f'❌ 阻断｜成功 {verified} 单，未处理 {pending} 单'
    return {'continued': f'接续｜已提交并验证 {verified} 单，按证据轮换',
            'stopped': f'已停止｜已提交并验证 {verified} 单',
            'preview': f"仅预览 {execution.get('preview_count', 0)} 单，未提交",
            'in_progress': '运行中｜等待本轮收尾',
            'legacy_unverified': '历史结果待核｜未保存执行快照'}[state]
