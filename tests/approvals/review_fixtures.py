import json

from apps.oa.approvals.store import Store


def completed_review(app, task_id):
    task = app.store.get_task(task_id)
    return {'task_id': task_id, 'material_sha256': task['material_sha256'],
            'status': 'complete', 'reason': 'Synthetic fixture review completed',
            'evidence_files': [task['material_file']]}


def completed_review_file(root, task_id):
    store = Store(root / 'approval.sqlite3', read_only=True)
    try:
        task = store.get_task(task_id)
    finally:
        store.connection.close()
    path = root / f'synthetic-review-{task_id}.json'
    path.write_text(json.dumps({'task_id': task_id, 'material_sha256': task['material_sha256'],
        'status': 'complete', 'reason': 'Synthetic fixture review completed',
        'evidence_files': [task['material_file']]}))
    return path


def recognition_report(task):
    from apps.oa.materials.reports import prepared_parts
    from apps.oa.materials.prepared import skipped_video_sources

    material = json.loads(task['material_json'])
    prepared = material.get('prepared') or {}
    parts = [{**metadata, 'answer': 'Synthetic external reading'} for metadata, _source in prepared_parts(prepared)]
    coverage = ('no_attachments' if material.get('attachment_presence', {}).get('status') == 'confirmed_empty' else
                'non_video_only' if skipped_video_sources(prepared.get('manifest', [])) else 'all_prepared_content')
    return {'schema_version': 1, 'kind': 'deepseek_attachment_recognition', 'status': 'complete' if parts else 'skipped',
            'completed_at': '2026-11-17T00:00:00Z', 'coverage': coverage,
            'parts': parts, 'read_plan': [part['part_id'] for part in parts],
            'material_binding': {'material_file': task['material_file'], 'material_sha256': task['material_sha256'],
                                 'request_id': task['request_id']}}


def collect_for_review(app, run_id, *, expected_task_id=None, agent_id=None, agent_workflow_key=None,
                       workflow_key=None, **options):
    app.store.run(run_id)
    existing = app.store.get_task(expected_task_id) if type(expected_task_id) is int else None
    if existing and existing['last_run_id'] == run_id and existing['status'] == 'prepared':
        result = {'status': 'prepared', 'task_id': expected_task_id, 'request_id': existing['request_id'],
                  'material_file': existing['material_file'], 'rule_file': existing['rule_file']}
    else:
        result = app.collect(run_id, expected_task_id=expected_task_id, **options)
    if result['status'] != 'prepared':
        return result
    task = app.store.get_task(result['task_id'])
    decision = app.dispatch_review(run_id, expected_task_id=expected_task_id, agent_id=agent_id,
        agent_workflow_key=agent_workflow_key, recognition=recognition_report(task)
        if task['workflow_key'] != 'overseas_invoice' else None)
    return result | decision if decision['status'] == 'reviewing' else decision


def deliver_and_submit(app, task_id, *, opinion=None, recognition=None, observations=None,
                       confirm_write=False, allow_overseas=False, review_assessment=None):
    task = app.store.get_task(task_id)
    if task['status'] == 'reviewing':
        if review_assessment is None and not confirm_write:
            review_assessment = completed_review(app, task_id)
        app.deliver(task_id, agent_id=task['agent_id'], material_file=task['material_file'],
            material_sha256=task['material_sha256'], opinion=opinion, recognition=recognition,
            observations=observations, review_assessment=review_assessment)
    return app.submit(task_id, confirm_write=confirm_write, allow_overseas=allow_overseas)


def review_cli(root, invoke, task_id, agent_id, workflow):
    store = Store(root / 'approval.sqlite3', read_only=True)
    try:
        task = store.get_task(int(task_id))
    finally:
        store.connection.close()
    if task['status'] == 'prepared':
        value = {'task_id': task['id'], 'request_id': task['request_id'], 'status': 'prepared',
                 'material_file': task['material_file'], 'rule_file': task['rule_file']}
    else:
        collected = invoke('collect', '--expected-task-id', str(task_id))
        value = collected[1] if isinstance(collected, tuple) else collected
        if value.get('status') != 'prepared':
            return collected
        store = Store(root / 'approval.sqlite3', read_only=True)
        try:
            task = store.get_task(int(task_id))
        finally:
            store.connection.close()
    report = recognition_report(task)
    path = root / f'synthetic-recognition-{task_id}.json'
    path.write_text(json.dumps(report))
    dispatched = invoke('dispatch-review', '--expected-task-id', str(task_id), '--agent-id', agent_id,
                        '--agent-workflow-key', workflow, '--recognition-file', str(path))
    result = dispatched[1] if isinstance(dispatched, tuple) else dispatched
    result = value | result if result.get('status') == 'reviewing' else result
    return (dispatched[0], result) if isinstance(dispatched, tuple) else result


def deliver_cli(root, invoke, task_id, opinion_file, *, confirm_write=False, recognition_file=None,
                review_file=None, operation='submit'):
    store = Store(root / 'approval.sqlite3', read_only=True)
    try:
        task = store.get_task(int(task_id))
    finally:
        store.connection.close()
    if task['status'] == 'prepared':
        path = root / f'synthetic-recognition-{task_id}.json'
        path.write_text(json.dumps(recognition_report(task)))
        invoke('dispatch-review', '--expected-task-id', str(task_id), '--agent-id', f'synthetic-{task_id}',
               '--agent-workflow-key', task['workflow_key'], '--recognition-file', str(recognition_file or path))
        store = Store(root / 'approval.sqlite3', read_only=True)
        try:
            task = store.get_task(int(task_id))
        finally:
            store.connection.close()
    if task['status'] == 'reviewing':
        assessment = review_file or completed_review_file(root, int(task_id))
        args = ['deliver', '--task-id', str(task_id), '--agent-id', task['agent_id'],
                '--material-file', task['material_file'], '--material-sha256', task['material_sha256'],
                '--opinion-file', str(opinion_file), '--review-file', str(assessment)]
        if recognition_file:
            args += ['--recognition-file', str(recognition_file)]
        saved = invoke(*args)
        value = saved[1] if isinstance(saved, tuple) else saved
        if value.get('status') != 'reviewed':
            return saved
    return invoke(operation, '--task-id', str(task_id), *(['--confirm-write', 'YES'] if confirm_write else []))


def review_input(root, requests):
    import io

    def expand():
        for request in requests:
            if request['operation'] == 'collect_for_review':
                yield {'operation': 'collect', 'expected_task_id': request['expected_task_id']}
                store = Store(root / 'approval.sqlite3', read_only=True)
                try:
                    task = store.get_task(request['expected_task_id'])
                finally:
                    store.connection.close()
                yield request | {'operation': 'dispatch-review', 'recognition': recognition_report(task)}
            elif request['operation'] == 'deliver_and_submit':
                store = Store(root / 'approval.sqlite3', read_only=True)
                try:
                    task = store.get_task(request['task_id'])
                finally:
                    store.connection.close()
                yield {'operation': 'deliver', 'task_id': task['id'], 'agent_id': task['agent_id'],
                       'material_file': task['material_file'], 'material_sha256': task['material_sha256'],
                       'opinion': request['opinion'], 'review_assessment': json.loads(completed_review_file(root, task['id']).read_text())}
                yield {'operation': request.get('writer_operation', 'submit'), 'task_id': task['id'],
                       'confirm_write': request.get('confirm_write', False)}
            else:
                yield request
    rows = iter(expand())
    class Input(io.StringIO):
        def readline(self, *args):
            value = next(rows, None)
            return json.dumps(value) + '\n' if value is not None else ''
    return Input()
