from tests.approvals.review_fixtures import collect_for_review, deliver_and_submit
from tests.approvals.review_fixtures import completed_review
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.oa.approvals import cli, receipts, retention
from tests.approvals.test_operations import service


def collected(app, agent='agent-a'):
    return collect_for_review(app, app.pending()['run_id'], agent_id=agent, expected_task_id=1,
                       agent_workflow_key='travel_expense')


def test_success_cleans_files_and_cache_but_keeps_history_and_repeat_guard(tmp_path):
    import sqlite3

    app, api = service(tmp_path)
    task = collected(app)
    report = tmp_path / 'recognitions' / 'case' / 'recognition.json'
    report.parent.mkdir(parents=True)
    report.write_text('{}')
    supplemental = tmp_path / 'recognitions' / 'reread' / 'recognition.json'
    supplemental.parent.mkdir()
    supplemental.write_text('{}')
    observations = supplemental.with_name('observations.json')
    observations.write_text('[]')
    checkpoint = tmp_path / 'collection-states' / '1' / 'synthetic.read.json'
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text('{}')
    with sqlite3.connect(tmp_path / 'attachment-cache.sqlite3') as cache:
        cache.execute('CREATE TABLE attachment_files(request_id,flow_key,resource_key,entry_json)')
        cache.execute("INSERT INTO attachment_files VALUES ('101','travel_expense','file-1','{}')")
    from tests.approvals.review_fixtures import recognition_report
    reading = recognition_report(app.store.get_task(1)) | {'report_file': str(report)}
    observation = {'resource_id': 'file-1', 'locator': 'reread', 'content': 'fixture',
                   'report_file': str(supplemental)}
    result = deliver_and_submit(app, 1, opinion='合成意见', recognition=reading, confirm_write=True,
                               observations=[observation], review_assessment=completed_review(app, 1))
    assert result['status'] == 'submitted' and result['cleanup']['files'] > 0
    assert not any(path.exists() for path in (Path(task['material_file']), report, supplemental, observations, checkpoint))
    details = app.inspect(1)
    assert details['attachments'] == details['contents'] == []
    assert details['task']['material_json'] is None
    assert details['reviews'][0]['recognition_json'] == '{}'
    assert details['reviews'][0]['opinion'] == '合成意见'
    assert details['submissions'][0]['status'] == 'submitted'
    assert deliver_and_submit(app, 1, opinion='禁止重发', confirm_write=True, review_assessment=completed_review(app, 1))['status'] == 'submitted'
    assert len(api.posts) == 1
    with sqlite3.connect(tmp_path / 'attachment-cache.sqlite3') as cache:
        assert cache.execute('SELECT count(*) FROM attachment_files').fetchone()[0] == 0
    assert retention.cleanup(app.store, execute=True)['files'] == 0


@pytest.mark.parametrize('status', ['preview', 'unknown', 'failed'])
def test_non_verified_tasks_keep_material(tmp_path, status):
    app, api = service(tmp_path)
    task = collected(app)
    if status != 'preview':
        api.submit_and_verify = lambda *_: {'failure_code': receipts.SUBMIT_REJECTED if status == 'failed' else 'writer_result_unknown'}
    result = deliver_and_submit(app, 1, opinion='合成意见', confirm_write=status != 'preview', review_assessment=completed_review(app, 1))
    assert result['status'] == status
    assert retention.cleanup(app.store, execute=True)['files'] == 0
    assert Path(task['material_file']).is_file()


def test_postcheck_success_cleans_only_after_verification(tmp_path):
    app, api = service(tmp_path)
    task = collected(app)
    api.submit_and_verify = lambda *_: {}
    assert deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))['status'] == 'unknown'
    api.rows = []
    api.handled = [{'request_id': '101', 'handled_at': '2999-01-01 00:00:00'}]
    result = app.postcheck(1)
    assert result['status'] == 'submitted'
    assert not Path(task['material_file']).exists()


def test_cleanup_preserves_shared_active_files_outside_files_and_database(tmp_path):
    app, _ = service(tmp_path)
    task = collected(app)
    original = next((Path(task['material_file']).parent / 'attachments').iterdir())
    deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text('共享原件')
    from tests.approvals.test_store import pending

    run = app.store.start_run([pending('102')])
    active = app.store.tasks(run)[0]
    with app.store.connection:
        app.store.connection.execute('UPDATE tasks SET frozen_json=? WHERE id=?',
                                     (json.dumps({str(original): 'digest'}), active['id']))
    outside = tmp_path.parent / 'unrelated.txt'
    outside.write_text('keep')
    reference = tmp_path / 'reference' / 'prices.json'
    reference.parent.mkdir()
    reference.write_text('{}')
    link = original.parent / 'linked.txt'
    link.symlink_to(outside)
    with app.store.connection:
        app.store.connection.execute('UPDATE tasks SET materials_cleanup_pending=1,frozen_json=? WHERE id=1',
            (json.dumps({str(original): 'digest', str(outside): 'digest', str(reference): 'digest',
                         str(app.store.path): 'digest'}),))
    result = retention.cleanup(app.store, execute=True)
    assert result['status'] == 'deferred' and result['protected_tasks'] == 1
    assert original.is_file() and outside.is_file() and link.is_symlink() and reference.is_file()
    assert app.store.get_task(1)['status'] == 'submitted'


def test_deletion_failure_preserves_verified_receipt_and_can_retry(tmp_path, monkeypatch):
    app, api = service(tmp_path)
    task = collected(app)
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path.name == 'material.json':
            raise PermissionError('synthetic')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail)
    result = deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))
    assert result['status'] == 'submitted' and result['cleanup']['status'] == 'partial'
    assert app.inspect(1)['submissions'][0]['status'] == 'submitted'
    run_id = app.store.latest_run()['id']
    closed = app.close(run_id)
    assert closed['cleanup']['pending_tasks'] == [{'task_id': 1, 'reason': 'file_cleanup_failed'}]
    from apps.oa.approvals.client import query

    assert query(tmp_path, 'status', run_id=run_id, view='batch')['cleanup'] == closed['cleanup']
    monkeypatch.setattr(Path, 'unlink', unlink)
    assert app.close(run_id)['cleanup']['status'] == 'completed'
    assert not Path(task['material_file']).exists()
    assert app.inspect(1)['task']['materials_cleanup_pending'] == 0
    assert len(api.posts) == 1


def test_expire_only_verified_history_older_than_one_year_and_not_seen_again(tmp_path):
    app, _ = service(tmp_path)
    collected(app)
    deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))
    run = app.store.latest_run()['id']
    app.close(run)
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    old = (cutoff - timedelta(seconds=1)).isoformat()
    with app.store.connection:
        app.store.connection.execute('UPDATE tasks SET last_seen_at=?', (old,))
        app.store.connection.execute('UPDATE requests SET last_seen_at=?', (old,))
        app.store.connection.execute('UPDATE submissions SET completed_at=?', (old,))
    assert app.store.expire_history(cutoff) == 1
    for table in ('tasks', 'attachments', 'attachment_contents', 'reviews', 'submissions', 'requests'):
        assert app.store.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    assert app.store.latest_run()['id'] == run
    assert app.store.connection.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('protected', ['unknown', 'queued', 'recent', 'seen_again', 'active_run'])
def test_expiration_preserves_unresolved_recent_or_current_history(tmp_path, protected):
    app, _ = service(tmp_path)
    collected(app)
    deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))
    if protected != 'active_run':
        app.close(app.store.latest_run()['id'])
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    old = (cutoff - timedelta(days=1)).isoformat()
    recent = (cutoff + timedelta(seconds=1)).isoformat()
    with app.store.connection:
        app.store.connection.execute('UPDATE tasks SET last_seen_at=?', (recent if protected == 'seen_again' else old,))
        app.store.connection.execute('UPDATE submissions SET completed_at=?', (recent if protected == 'recent' else old,))
        if protected in {'unknown', 'queued'}:
            app.store.connection.execute('UPDATE tasks SET status=?', (protected,))
    assert app.store.expire_history(cutoff) == 0
    assert app.store.get_task(1)


def test_cli_preview_does_not_mutate_and_execute_requires_idle_owner(tmp_path, capsys):
    from apps.oa.approvals.owner import round_lock

    app, _ = service(tmp_path)
    task = collected(app)
    deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))
    material = Path(task['material_file'])
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text('{}')
    with app.store.connection:
        app.store.connection.execute('ALTER TABLE tasks DROP COLUMN materials_cleanup_pending')
        app.store.connection.execute('ALTER TABLE tasks DROP COLUMN recognition_json')
        app.store.connection.execute('ALTER TABLE reviews DROP COLUMN delivery_json')
    app.store.connection.close()
    database = tmp_path / 'approval.sqlite3'
    before = database.stat().st_mtime_ns
    assert cli.main(['--root', str(tmp_path), 'cleanup']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['status'] == 'preview'
    assert plan['tasks'] == plan['files'] == 1
    assert plan['allocated_bytes'] == material.stat().st_blocks * 512
    assert plan['items'][0]['files'] == [str(material.relative_to(tmp_path))]
    assert database.stat().st_mtime_ns == before
    assert material.exists()
    with round_lock(tmp_path):
        assert cli.main(['--root', str(tmp_path), 'cleanup', '--execute']) == 1
        assert json.loads(capsys.readouterr().out)['error'] == 'BlockingIOError'
    assert material.exists()
    assert cli.main(['--root', str(tmp_path), 'cleanup', '--execute']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'completed'
    assert not material.exists()


def test_new_delivery_collects_fresh_material_after_previous_cleanup(tmp_path):
    app, api = service(tmp_path)
    previous = dict(api.rows[0])
    first = collected(app)
    deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))
    api.rows = [{**previous, 'received_at': '2026-10-23 09:00:00'}]
    run = app.pending()['run_id']
    second = collect_for_review(app, run, agent_id='agent-b', expected_task_id=2, agent_workflow_key='travel_expense')
    assert second['task_id'] != first['task_id']
    assert Path(second['material_file']).is_file()
    assert not Path(first['material_file']).exists()
    assert len(app.inspect(1)['submissions']) == 1


def test_unverified_submitted_flag_and_collection_in_progress_prevent_cleanup(tmp_path):
    app, _ = service(tmp_path)
    task = collected(app)
    app.store.set_status(1, 'submitted')
    assert retention.cleanup(app.store, execute=True)['files'] == 0
    assert Path(task['material_file']).is_file()
    app.store.set_status(1, 'preparing')
    assert retention.cleanup(app.store, execute=True) == {'status': 'deferred', 'reason': 'collection_in_progress'}
    assert Path(task['material_file']).is_file()


def test_symlinked_materials_parent_does_not_remove_outside_empty_directories(tmp_path):
    app, _ = service(tmp_path)
    collected(app)
    deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))
    materials = tmp_path / 'materials'
    materials.rmdir()
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    empty = outside / '1' / 'empty'
    empty.mkdir(parents=True)
    materials.symlink_to(outside, target_is_directory=True)
    with app.store.connection:
        app.store.connection.execute('UPDATE tasks SET materials_cleanup_pending=1 WHERE id=1')
    result = retention.cleanup(app.store, execute=True)
    assert result['status'] == 'completed' and result['tasks'] == 1
    assert empty.is_dir()


def test_successful_retry_cleans_previous_failed_attempt_and_keeps_receipt(tmp_path):
    from tests.approvals.test_operations import collect

    app, api = service(tmp_path)

    def failed(request_id, **kwargs):
        collect(request_id, **kwargs)
        raise RuntimeError('attachment_content_incomplete')

    app.collector = failed
    run = app.pending()['run_id']
    with pytest.raises(RuntimeError) as captured:
        collect_for_review(app, run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    receipt = Path(captured.value.diagnostics_file)
    failed_directory = Path(json.loads(receipt.read_text())['material_directory'])
    original = next((failed_directory / 'attachments').iterdir())
    assert original.is_file()
    app.collector = collect
    retry_run = app.pending(request_ids=['101'], retry_failed=True)['run_id']
    retried = collect_for_review(app, retry_run, agent_id='agent-b', expected_task_id=1, agent_workflow_key='travel_expense')
    assert deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))['status'] == 'submitted'
    assert len(api.posts) == 1
    assert retention.cleanup(app.store, execute=True)['files'] == 0
    assert not original.exists() and not receipt.exists()
    assert not Path(retried['material_file']).exists()
    assert app.inspect(1)['reviews'][0]['opinion'] == '合成意见'
    assert app.inspect(1)['submissions'][0]['status'] == 'submitted'


def test_rejected_submission_evidence_survives_successful_retry(tmp_path):
    app, api = service(tmp_path)
    failed = collected(app)
    writer = api.submit_and_verify
    api.submit_and_verify = lambda *_: {'failure_code': receipts.SUBMIT_REJECTED}
    assert deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))['status'] == 'failed'

    def same_material_retry(*args, **kwargs):
        review = app.store.latest_review(1)
        with app.store.connection:
            app.store.connection.execute("UPDATE reviews SET material_sha256=?,recognition_json=? WHERE id!=?",
                                         (review['material_sha256'], '{"different_recognition": true}', review['id']))
        return writer(*args, **kwargs)

    api.submit_and_verify = same_material_retry
    retry_run = app.pending(request_ids=['101'], retry_failed=True)['run_id']
    retried = collect_for_review(app, retry_run, agent_id='agent-b', expected_task_id=1, agent_workflow_key='travel_expense')
    assert deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True, review_assessment=completed_review(app, 1))['status'] == 'submitted'
    assert retention.cleanup(app.store, execute=True)['files'] == 0
    assert not Path(failed['material_file']).exists() and not Path(retried['material_file']).exists()
    assert [row['status'] for row in app.inspect(1)['submissions']] == ['failed', 'submitted']
    assert [json.loads(row['recognition_json']) for row in app.inspect(1)['reviews']] == [{'review_id': 1}, {}]


def test_automatic_sweep_leaves_unapproved_history_and_cleans_new_completion(tmp_path):
    app, api = service(tmp_path)
    previous = dict(api.rows[0])
    first = collected(app)
    with patch.object(retention, 'cleanup', return_value={'status': 'deferred'}):
        assert deliver_and_submit(app, 1, opinion='历史意见', confirm_write=True,
                                  review_assessment=completed_review(app, 1))['status'] == 'submitted'
    with app.store.connection:
        app.store.connection.execute('UPDATE tasks SET materials_cleanup_pending=NULL WHERE id=1')
    run = app.store.latest_run()['id']
    app.close(run)
    assert Path(first['material_file']).exists()

    api.rows = [{**previous, 'received_at': '2026-10-23 09:00:00'}]
    next_run = app.pending()['run_id']
    second = collect_for_review(app, next_run, agent_id='agent-b', expected_task_id=2,
                                agent_workflow_key='travel_expense')
    assert deliver_and_submit(app, 2, opinion='新意见', confirm_write=True,
                              review_assessment=completed_review(app, 2))['status'] == 'submitted'
    assert not Path(second['material_file']).exists()
    assert Path(first['material_file']).exists()
    preview = retention.cleanup(app.store, history=True)
    assert [item['task_id'] for item in preview['items']] == [1]
    assert preview['files'] > 0


def test_reappeared_submitted_occurrence_blocks_deferred_cleanup(tmp_path):
    app, api = service(tmp_path)
    previous = dict(api.rows[0])
    task = collected(app)
    with patch.object(retention, 'cleanup', return_value={'status': 'deferred'}):
        assert deliver_and_submit(app, 1, opinion='合成意见', confirm_write=True,
                                  review_assessment=completed_review(app, 1))['status'] == 'submitted'
    api.rows = [previous]
    app.pending()
    assert app.store.get_task(1)['error'] == 'submitted_pending_confirmation'
    assert Path(task['material_file']).exists()
    assert retention.cleanup(app.store, history=True)['items'] == []


def test_closed_stopped_collection_reports_and_allows_later_sweep(tmp_path):
    app, api = service(tmp_path)
    api.rows.append({**api.rows[0], 'request_id': '102'})
    collected_task = collected(app)
    run_id = collected_task['run_id']
    with patch.object(retention, 'cleanup', return_value={'status': 'deferred'}):
        deliver_and_submit(app, 1, opinion='Synthetic approval', confirm_write=True,
                           review_assessment=completed_review(app, 1))
    assert app.store.claim(run_id, expected_task_id=2)['status'] == 'claimed'
    partial = tmp_path / 'materials' / '2' / 'stopped' / 'attachment.txt'
    partial.parent.mkdir(parents=True)
    partial.write_text('Synthetic partial download')
    closed = app.store.close(run_id, collection_stopped_note='Synthetic owner stopped', closure={'kind': 'orphaned'})
    assert closed['cleanup']['pending_tasks'] == [{'task_id': 1, 'reason': 'awaiting_cleanup_sweep'}]
    assert Path(collected_task['material_file']).exists()

    app.pending(request_ids=['103'])
    assert not Path(collected_task['material_file']).exists()
    assert partial.read_text() == 'Synthetic partial download'
