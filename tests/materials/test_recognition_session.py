from __future__ import annotations

import json
import hashlib
from pathlib import Path

from PIL import Image

from apps.oa.materials import recognize as recognition


def test_partial_failure_keeps_completed_checkpoint_and_single_request_budget(tmp_path, monkeypatch):
    files = []
    for number, color in enumerate(('red', 'blue'), 1):
        path = tmp_path / f'{number}.png'
        Image.new('RGB', (3, 2), color).save(path)
        files.append(path)
    output = tmp_path / 'report'
    ticks = iter((10, 12, 13, 14, 15, 16, 17))
    monkeypatch.setattr(recognition.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-api-key')
    requests = []

    def post(body, *, key, timeout):
        checkpoint = json.loads((output / 'recognition.json').read_text())
        requests.append((timeout, checkpoint))
        return {'choices': [{'finish_reason': 'stop' if len(requests) == 1 else 'length',
                             'message': {'content': 'visible text'}}]}

    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(files=files, output_dir=output, budget_seconds=10)

    assert [timeout for timeout, _ in requests] == [6, 3]
    assert requests[0][1]['status'] == 'running'
    assert requests[0][1]['parts'] == []
    assert [part['part_id'] for part in requests[1][1]['parts']] == ['image-1']
    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_response_incomplete'
    assert [part['part_id'] for part in report['parts']] == ['image-1']
    assert report['failed_part']['part_id'] == 'image-2'
    assert report['failed_response']['finish_reason'] == 'length'
    assert json.loads(Path(report['report_file']).read_text()) == report


def test_missing_model_key_precedes_legacy_skip_metadata_error(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    Image.new('RGB', (3, 2), 'red').save(image)
    material = tmp_path / 'material.json'
    material.write_text(json.dumps({'prepared': {
        'images': [{'resource_id': 'page', 'path': str(image),
                    'content_sha256': hashlib.sha256(image.read_bytes()).hexdigest()}],
        'manifest': [{'resource_id': 'page', 'status': 'image', 'image_count': 1,
                      'skipped_videos': None}], 'structured_contents': []}}))
    monkeypatch.setenv('OA_REVIEW_API_KEY', '')

    report = recognition.recognize(material_file=material, output_dir=tmp_path / 'report')

    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_api_key_missing'
    assert report['material_binding']['material_file'] == str(material)
    assert 'skipped_sources' not in report
    assert 'coverage' not in report
