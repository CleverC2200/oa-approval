import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize('originals_removed', [False, True])
@pytest.mark.parametrize('workflow_key,fields_match,verified', [
    ('travel_expense', None, True), ('overseas_invoice', True, True),
    ('overseas_invoice', False, False), ('overseas_invoice', None, False),
])
def test_readonly_timing_tool_uses_delivered_paths_and_preserved_files(tmp_path, originals_removed, workflow_key, fields_match, verified):
    case = tmp_path / 'case'
    version = tmp_path / 'version'
    preserved = tmp_path / 'preserved'
    for folder in (case, version, preserved):
        folder.mkdir()
    files = []
    delivery = {'run_id': 'synthetic-run', 'task_id': 1}
    contents = {
        'material_file': ('material.json', {'kind': 'synthetic'}),
        'review_file': ('review.json', {'task_id': 1, 'status': 'business_pending'}),
        'opinion_file': ('signoff.txt', 'Synthetic opinion'),
        'recognition_file': ('recognition.json', {'status': 'complete',
            'created_at': '1970-01-01T00:16:40+00:00', 'completed_at': '1970-01-01T00:16:42+00:00'}),
    }
    for key, (name, value) in contents.items():
        source = version / name
        source.write_text(value if isinstance(value, str) else json.dumps(value))
        os.utime(source, (1100, 1100))
        target = preserved / name
        shutil.copy2(source, target)
        files.append({'original': str(source), 'preserved': str(target), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
        delivery[key] = str(source)
    manifest = preserved / 'manifest.json'
    manifest.write_text(json.dumps({'run_id': 'synthetic-run', 'task_id': 1,
        'review_file': delivery['review_file'], 'files': files}))
    (case / 'checkpoint.json').write_text(json.dumps({'run_id': 'synthetic-run'}))
    (case / 'delivery-1.json').write_text(json.dumps(delivery))
    collected = case / 'collect-1.json'
    collected.write_text(json.dumps({'run_id': 'synthetic-run', 'task_id': 1, 'request_id': 'synthetic-request',
        'workflow_key': workflow_key, 'material_file': delivery['material_file']}))
    os.utime(collected, (1000, 1000))
    checkpoint = case / 'preserved-1.json'
    checkpoint.write_text(json.dumps({'manifest': str(manifest)}))
    os.utime(checkpoint, (1900, 1900))
    receipt = case / 'submit-1.json'
    receipt.write_text(json.dumps({'task_id': 1, 'status': 'submitted',
        'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True, 'fields_match': fields_match}))
    os.utime(receipt, (2000, 2000))
    if originals_removed:
        shutil.rmtree(version)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    tool = Path(__file__).resolve().parents[2] / '.codex/approval_timings.py'
    result = subprocess.run([sys.executable, str(tool), '--case-dir', str(case)], capture_output=True, text=True)
    if verified:
        assert result.returncode == 0, result.stderr
        row, = json.loads(result.stdout)['tasks']
        assert row['recognition_seconds'] == 2
        assert row['material_ready_to_review_saved_seconds'] == row['material_ready_to_opinion_saved_seconds'] == 100
        assert row['pre_submit_to_verified_receipt_seconds'] == 100
    else:
        assert result.returncode != 0 and 'timing_submission_not_verified' in result.stderr
    assert {str(path): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before
