from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from apps.oa.approvals import cli, receipts, retention
from tests.approvals.test_operations import service


def collected(app, agent='agent-a'):
    return app.collect(app.pending()['run_id'], agent_id=agent, expected_task_id=1,
                       agent_workflow_key='travel_expense')


def test_success_cleans_files_and_cache_but_keeps_history_and_repeat_guard(tmp_path):
    import sqlite3

    app, api = service(tmp_path)
    task = collected(app)
    report = tmp_path / 'recognitions' / 'case' / 'recognition.json'
    report.parent.mkdir(parents=True)
    report.write_text('{}')
    with sqlite3.connect(tmp_path / 'attachment-cache.sqlite3') as cache:
        cache.execute('CREATE TABLE attachment_files(request_id,flow_key,resource_key,entry_json)')
        cache.execute("INSERT INTO attachment_files VALUES ('101','travel_expense','file-1','{}')")
    result = app.submit(1, opinion='合成意见', recognition={'report_file': str(report)}, confirm_write=True)
    assert result['status'] == 'submitted' and result['cleanup']['files'] > 0
    assert not Path(task['material_file']).exists() and not report.exists()
    details = app.inspect(1)
    assert details['contents'] and details['task']['material_json']
    assert details['reviews'][0]['opinion'] == '合成意见'
    assert details['submissions'][0]['status'] == 'submitted'
    assert app.submit(1, opinion='禁止重发', confirm_write=True)['status'] == 'submitted'
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
    result = app.submit(1, opinion='合成意见', confirm_write=status != 'preview')
    assert result['status'] == status
    assert retention.cleanup(app.store, execute=True)['files'] == 0
    assert Path(task['material_file']).is_file()


def test_postcheck_success_cleans_only_after_verification(tmp_path):
    app, api = service(tmp_path)
    task = collected(app)
    api.submit_and_verify = lambda *_: {}
    assert app.submit(1, opinion='合成意见', confirm_write=True)['status'] == 'unknown'
    api.rows = []
    api.handled = [{'request_id': '101', 'handled_at': '2999-01-01 00:00:00'}]
    result = app.postcheck(1)
    assert result['status'] == 'submitted'
    assert not Path(task['material_file']).exists()


def test_cleanup_preserves_shared_active_files_outside_files_and_database(tmp_path):
    app, _ = service(tmp_path)
    task = collected(app)
    original = next((Path(task['material_file']).parent / 'attachments').iterdir())
    app.submit(1, opinion='合成意见', confirm_write=True)
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
        app.store.connection.execute('UPDATE tasks SET frozen_json=? WHERE id=1',
            (json.dumps({str(original): 'digest', str(outside): 'digest', str(reference): 'digest',
                         str(app.store.path): 'digest'}),))
    result = retention.cleanup(app.store, execute=True)
    assert result['status'] == 'completed'
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
    result = app.submit(1, opinion='合成意见', confirm_write=True)
    assert result['status'] == 'submitted' and result['cleanup']['status'] == 'partial'
    assert app.inspect(1)['submissions'][0]['status'] == 'submitted'
    monkeypatch.setattr(Path, 'unlink', unlink)
    assert retention.cleanup(app.store, execute=True)['status'] == 'completed'
    assert not Path(task['material_file']).exists()
    assert len(api.posts) == 1


def test_expire_only_verified_history_older_than_one_year_and_not_seen_again(tmp_path):
    app, _ = service(tmp_path)
    collected(app)
    app.submit(1, opinion='合成意见', confirm_write=True)
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
    app.submit(1, opinion='合成意见', confirm_write=True)
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
    app.submit(1, opinion='合成意见', confirm_write=True)
    material = Path(task['material_file'])
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text('{}')
    assert cli.main(['--root', str(tmp_path), 'cleanup']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'preview'
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
    app.submit(1, opinion='合成意见', confirm_write=True)
    api.rows = [{**previous, 'received_at': '2026-10-23 09:00:00'}]
    run = app.pending()['run_id']
    second = app.collect(run, agent_id='agent-b', expected_task_id=2, agent_workflow_key='travel_expense')
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
    app.submit(1, opinion='合成意见', confirm_write=True)
    materials = tmp_path / 'materials'
    materials.rmdir()
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    empty = outside / '1' / 'empty'
    empty.mkdir(parents=True)
    materials.symlink_to(outside, target_is_directory=True)
    assert retention.cleanup(app.store, execute=True)['status'] == 'completed'
    assert empty.is_dir()
