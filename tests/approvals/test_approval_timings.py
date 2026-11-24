import json
from pathlib import Path
import subprocess
import sys

import pytest

from apps.oa.approvals.receipts import verification_result
from tests.approvals.review_fixtures import collect_for_review, completed_review, deliver_and_submit, recognition_report
from tests.approvals.test_operations import service


@pytest.mark.parametrize('verified', [False, True])
def test_closed_run_timing_uses_saved_events_and_receipts(tmp_path, verified):
    app, api = service(tmp_path)
    run_id = app.pending()['run_id']
    task = collect_for_review(app, run_id, agent_id='timing-reviewer', expected_task_id=1,
                              agent_workflow_key='travel_expense')
    material = Path(task['material_file'])
    work_file = material.parent / 'diagnostics' / 'supplemental.json'
    work_file.parent.mkdir()
    work_file.write_text('{"synthetic": true}')
    review = completed_review(app, 1)
    recognition = recognition_report(app.store.get_task(1)) | {
        'created_at': '2026-11-24T12:00:00+00:00', 'completed_at': '2026-11-24T12:00:02+00:00',
    }
    if not verified:
        api.submit_and_verify = lambda *_: verification_result(False, True, submit_ok=False)
    result = deliver_and_submit(app, 1, opinion='Synthetic approved opinion', confirm_write=True,
                                recognition=recognition, review_assessment=review)
    assert result['status'] == ('submitted' if verified else 'unknown')
    outcome = app.close(run_id)
    assert outcome['cleanup']['status'] == 'completed'
    assert material.exists() is not verified
    assert work_file.exists() is not verified

    tool = Path(__file__).resolve().parents[2] / '.codex' / 'approval_timings.py'
    command = [sys.executable, str(tool), '--root', str(tmp_path), '--run-id', run_id]
    before = (tmp_path / 'approval.sqlite3').stat().st_mtime_ns
    report = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
    row, = report['tasks']
    assert row['recognition_seconds'] == 2
    assert row['material_ready_to_review_saved_seconds'] >= 0
    assert row['submission_verified'] is verified
    if verified:
        assert row['pre_submit_to_verified_receipt_seconds'] >= 0
    else:
        assert row['pre_submit_to_verified_receipt_seconds'] is None
    assert (tmp_path / 'approval.sqlite3').stat().st_mtime_ns == before
    app.store.connection.close()
