from apps.oa.approvals.store import Store
from tests.approvals.test_store import pending


def test_preparation_has_one_independent_buffer_and_no_reviewer_identity(tmp_path):
    store = Store(tmp_path / 'approval.sqlite3')
    run = store.start_run([pending(str(value)) for value in range(101, 106)])
    from tests.approvals.test_store import freeze

    for task_id in range(1, 4):
        freeze(store, run, task_id)
        assert store.get_task(task_id)['status'] == 'prepared'
        assert store.dispatch_review(run, expected_task_id=task_id, agent_id=f'real-{task_id}',
                                     agent_workflow_key='travel_expense')['status'] == 'claimed'
    assert store.claim(run, expected_task_id=4)['status'] == 'claimed'
    assert store.get_task(4)['agent_id'] is None
    assert store.claim(run, expected_task_id=5)['reason'] == 'preparation_buffer_full'


def prepared(app, run, task_id):
    result = app.collect(run, expected_task_id=task_id)
    assert result['status'] == 'prepared'
    return result


def reviewing(app, run, task_id, agent):
    result = app.dispatch_review(run, expected_task_id=task_id, agent_id=agent,
                                agent_workflow_key='travel_expense')
    assert result['status'] == 'reviewing'
    return result


def delivery(app, task_id, **changes):
    from tests.approvals.review_fixtures import completed_review

    task = app.store.get_task(task_id)
    options = dict(agent_id=task['agent_id'], material_file=task['material_file'],
                   material_sha256=task['material_sha256'], opinion='Synthetic complete opinion',
                   review_assessment=completed_review(app, task_id))
    return app.deliver(task_id, **(options | changes))


def test_atomic_delivery_frees_review_slot_and_reserves_every_inflight_result(tmp_path):
    from tests.approvals.test_operations import service

    app, api = service(tmp_path)
    api.rows = [pending(str(value)) for value in range(101, 108)]
    run = app.pending()['run_id']
    for task_id in range(1, 4):
        prepared(app, run, task_id)
        reviewing(app, run, task_id, f'real-{task_id}')
    prepared(app, run, 4)
    assert app.dispatch_review(run, expected_task_id=4, agent_id='real-4',
                              agent_workflow_key='travel_expense')['reason'] == 'inflight_limit'
    assert delivery(app, 1)['status'] == 'reviewed'
    reviewing(app, run, 4, 'real-1')
    prepared(app, run, 5)
    delivery(app, 2)
    reviewing(app, run, 5, 'real-2')
    prepared(app, run, 6)
    delivery(app, 3)
    assert app.dispatch_review(run, expected_task_id=6, agent_id='real-3',
                              agent_workflow_key='travel_expense')['reason'] == 'result_backpressure'
    delivery(app, 4)
    delivery(app, 5)
    capacity = app.store.pipeline_capacity(app.store.tasks(run))
    assert capacity['results'] == 5
    assert capacity['result_reserved'] == 0 and capacity['result_capacity'] == 6
    assert capacity['backpressure'] is True
    app.store.connection.close()
    reopened = Store(tmp_path / 'approval.sqlite3')
    assert reopened.get_task(5)['status'] == 'reviewed'
    details = reopened.details(5)
    assert len(details['reviews']) == len(details['review_assessments']) == 1
    assert details['reviews'][0]['opinion'] == 'Synthetic complete opinion'
    assert api.posts == []


def test_active_writer_does_not_block_preparation_or_three_reviewers(tmp_path):
    from threading import Event

    from tests.approvals.test_operations import service

    app, api = service(tmp_path)
    api.rows = [pending(str(value)) for value in range(101, 106)]
    run = app.pending()['run_id']
    prepared(app, run, 1)
    reviewing(app, run, 1, 'real-1')
    delivery(app, 1)
    started, release = Event(), Event()
    original = api.submit_and_verify
    def slow_writer(request_id, opinion):
        started.set()
        assert release.wait(10)
        return original(request_id, opinion)
    api.submit_and_verify = slow_writer
    def submit():
        other, _api = service(tmp_path, api=api)
        try:
            return other.submit(1, confirm_write=True)
        finally:
            other.store.connection.close()
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as writer:
        future = writer.submit(submit)
        try:
            assert started.wait(5)
            for task_id in range(2, 5):
                prepared(app, run, task_id)
                reviewing(app, run, task_id, f'real-{task_id}')
            assert app.store.pipeline_capacity(app.store.tasks(run))['reviewing'] == 3
            assert app.store.get_task(1)['status'] == 'submitting'
        finally:
            release.set()
        assert future.result(timeout=10)['status'] == 'submitted'


def test_unknown_submission_blocks_all_runtime_dispatch_and_local_delivery_writes(tmp_path):
    import pytest

    from tests.approvals.test_operations import service

    app, api = service(tmp_path)
    api.rows = [pending(str(value)) for value in range(101, 104)]
    run = app.pending()['run_id']
    for task_id in (1, 2):
        prepared(app, run, task_id)
        reviewing(app, run, task_id, f'real-{task_id}')
    delivery(app, 1)
    api.submit_and_verify = lambda *_args: {'submit_ok': False, 'postcheck_done_hit': False}
    assert app.submit(1, confirm_write=True)['status'] == 'unknown'
    before = app.inspect(2)
    files = {str(path): path.read_bytes() for path in (tmp_path / 'materials').rglob('*') if path.is_file()}
    with pytest.raises(ValueError, match='run_blocked'):
        delivery(app, 2, observations=[{'status': 'failed'}])
    assert app.inspect(2) == before
    assert files == {str(path): path.read_bytes() for path in (tmp_path / 'materials').rglob('*') if path.is_file()}
    fresh = app.pending()['run_id']
    assert app.collect(fresh, expected_task_id=2)['reason'] == 'run_blocked'
    assert app.dispatch_review(fresh, expected_task_id=2, agent_id='new-real',
                              agent_workflow_key='travel_expense')['reason'] == 'run_blocked'


def test_delivery_retry_returns_same_persisted_receipt_without_extra_evidence_or_reviews(tmp_path):
    from pathlib import Path

    from tests.approvals.test_operations import service

    app, api = service(tmp_path)
    run = app.pending()['run_id']
    prepared(app, run, 1)
    reviewing(app, run, 1, 'real-1')
    first = delivery(app, 1)
    before = {str(path): path.read_bytes() for path in Path(first['evidence_files'][0]).parent.rglob('*') if path.is_file()}
    repeated = delivery(app, 1)
    assert repeated == first
    assert len(app.inspect(1)['reviews']) == len(app.inspect(1)['review_assessments']) == 1
    assert before == {str(path): path.read_bytes() for path in Path(first['evidence_files'][0]).parent.rglob('*') if path.is_file()}
    assert api.posts == []


def test_delivery_validates_identity_version_and_rolls_back_all_evidence_facts(tmp_path):
    import pytest

    from tests.approvals.test_operations import service

    app, _api = service(tmp_path)
    run = app.pending()['run_id']
    prepared(app, run, 1)
    reviewing(app, run, 1, 'real-1')
    for changes, reason in [({'agent_id': 'other-real'}, 'review_agent_mismatch'),
                            ({'material_sha256': 'stale'}, 'review_material_mismatch'),
                            ({'material_file': '/other/material.json'}, 'review_material_mismatch'),
                            ({'observations': [{'resource_id': 'other', 'locator': 'page:1', 'content': 'Synthetic'}]},
                             'observation_resource_unbound')]:
        with pytest.raises(ValueError, match=reason):
            delivery(app, 1, **changes)
        details = app.inspect(1)
        assert details['reviews'] == details['review_assessments'] == []
        assert details['task']['status'] == 'reviewing'
        assert app.store.pipeline_capacity(app.store.tasks(run))['reviewing'] == 1
    assert delivery(app, 1)['status'] == 'reviewed'


def test_dispatch_requires_complete_preparation_and_current_full_recognition(tmp_path):
    from copy import deepcopy

    import pytest

    from apps.oa.approvals.demo import create_demo
    from tests.approvals.review_fixtures import recognition_report

    app = create_demo(tmp_path)
    run = app.pending()['run_id']
    prepared(app, run, 1)
    current = app.store.get_task(1)
    complete = recognition_report(current)
    invalid = []
    wrong_version = deepcopy(complete)
    wrong_version['material_binding']['material_sha256'] = 'stale'
    invalid.append((wrong_version, 'recognition_material_mismatch'))
    selected = deepcopy(complete) | {'coverage': 'selected_resource', 'selection': {'resource_id': 'demo-pdf'}}
    invalid.append((selected, 'recognition_coverage_incomplete'))
    partial = deepcopy(complete)
    partial['parts'].pop()
    partial['read_plan'].pop()
    invalid.append((partial, 'recognition_coverage_incomplete'))
    foreign = deepcopy(complete)
    foreign['parts'][0]['resource_id'] = 'different-resource'
    invalid.append((foreign, 'recognition_source_mismatch'))
    invalid.extend([(None, 'recognition_not_complete'), (deepcopy(complete) | {'status': 'running'}, 'recognition_not_complete')])
    for report, reason in invalid:
        with pytest.raises(ValueError, match=reason):
            app.dispatch_review(run, expected_task_id=1, agent_id='real-1',
                                agent_workflow_key='travel_expense', recognition=report)
        task = app.store.get_task(1)
        assert task['status'] == 'prepared' and task['agent_id'] is None
        assert task['failure_count'] == 0
    assert app.dispatch_review(run, expected_task_id=1, agent_id='real-1',
                               agent_workflow_key='travel_expense', recognition=complete)['status'] == 'reviewing'


def test_delivery_is_local_and_preserves_independent_evidence_before_writer_reads(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from apps.oa.approvals import finance_precheck
    from tests.approvals.review_fixtures import completed_review
    from tests.approvals.test_operations import service

    app, api = service(tmp_path)
    run = app.pending()['run_id']
    prepared(app, run, 1)
    reviewing(app, run, 1, 'real-1')
    evidence = Path(app.store.get_task(1)['material_file']).parent / 'independent-evidence.txt'
    evidence.write_text('Synthetic row 1: quantity 2 at price 3; total 6')
    assessment = completed_review(app, 1) | {'evidence_files': [str(evidence)]}
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Local delivery must not read remote sources')
    monkeypatch.setattr(finance_precheck, 'load_policy', forbidden)
    app.predictor = api.pending_rows = api.handled_rows = forbidden
    result = delivery(app, 1, review_assessment=assessment)
    assert Path(result['evidence_files'][0]).read_text() == evidence.read_text()
    evidence.write_text('Original working note edited after delivery')
    saved = app.inspect(1)['reviews'][0]
    saved_assessment = json.loads(saved['delivery_json'])['assessment']
    assert Path(saved_assessment['evidence_files'][0]).read_text() == 'Synthetic row 1: quantity 2 at price 3; total 6'
    assert app.store.pipeline_capacity(app.store.tasks(run))['reviewing'] == 0


def test_cross_run_unknown_remains_blocked_after_readonly_resolution(tmp_path):
    from tests.approvals.test_operations import service

    app, api = service(tmp_path)
    api.rows = [pending('101'), pending('102')]
    original = app.pending()['run_id']
    prepared(app, original, 1)
    reviewing(app, original, 1, 'real-1')
    delivery(app, 1)
    api.submit_and_verify = lambda *_args: {}
    assert app.submit(1, confirm_write=True)['status'] == 'unknown'
    fresh = app.pending()['run_id']
    assert app.store.run(fresh)['status'] == 'blocked'
    api.rows = [row for row in api.rows if row['request_id'] != '101']
    api.handled = [{'request_id': '101', 'handled_at': '2999-01-01 00:00:00'}]
    assert app.postcheck(1)['status'] == 'submitted'
    assert app.store.run(fresh)['status'] == 'blocked'
    assert app.collect(fresh, expected_task_id=2)['reason'] == 'run_blocked'


def test_writer_reservation_is_single_and_bound_to_the_saved_task_review(tmp_path):
    import pytest

    from tests.approvals.test_operations import service

    app, api = service(tmp_path)
    api.rows = [pending('101'), pending('102')]
    run = app.pending()['run_id']
    reviews = []
    for task_id in (1, 2):
        prepared(app, run, task_id)
        reviewing(app, run, task_id, f'real-{task_id}')
        reviews.append(delivery(app, task_id)['review_id'])
    with pytest.raises(ValueError, match='review_mismatch'):
        app.store.begin_submission(1, reviews[1])
    started = app.store.begin_submission(1, reviews[0])
    other = Store(tmp_path / 'approval.sqlite3')
    with pytest.raises(ValueError, match='writer_busy'):
        other.begin_submission(2, reviews[1])
    assert other.get_task(2)['status'] == 'reviewed'
    assert other.details(2)['submissions'] == []
    app.store.finish_submission(started, 'submitted', {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True})
    assert other.begin_submission(2, reviews[1]) > started
    other.connection.close()


def test_late_failure_assessment_cannot_change_a_blocked_or_delivered_review(tmp_path):
    import pytest

    from tests.approvals.review_fixtures import completed_review
    from tests.approvals.test_operations import service

    app, api = service(tmp_path)
    run = app.pending()['run_id']
    prepared(app, run, 1)
    reviewing(app, run, 1, 'real-1')
    task = app.store.get_task(1)
    failure = completed_review(app, 1) | {'status': 'technical_failure'}
    delivery(app, 1)
    with pytest.raises(ValueError, match='task_not_reviewable'):
        app.store.assess_review(task, failure)
    assert len(app.inspect(1)['review_assessments']) == 1
    api.submit_and_verify = lambda *_args: {}
    assert app.submit(1, confirm_write=True)['status'] == 'unknown'
    before = app.inspect(1)
    with pytest.raises(ValueError, match='run_blocked'):
        app.store.assess_review(task, failure)
    assert app.inspect(1) == before
