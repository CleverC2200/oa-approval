from __future__ import annotations

import base64
import hashlib
from io import BytesIO

import pytest
from PIL import Image

from apps.oa.materials import recognize as recognition


@pytest.mark.parametrize('rotate', [0, 90])
def test_truncated_page_reads_overlapping_regions_before_completing(tmp_path, monkeypatch, rotate):
    image = tmp_path / 'page.png'
    Image.new('RGB', (400, 600), 'red').save(image)
    original = image.read_bytes()
    requests = []

    def post(body, **kwargs):
        content = body['messages'][1]['content']
        raw = base64.b64decode(content[1]['image_url']['url'].split(',', 1)[1])
        with Image.open(BytesIO(raw)) as page:
            requests.append(page.size)
        return {'choices': [{'finish_reason': 'length' if len(requests) == 1 else 'stop',
                             'message': {'content': 'discarded' if len(requests) == 1 else 'complete region'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}}

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake')
    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(files=[image], rotate=rotate, output_dir=tmp_path / 'report')

    assert report['status'] == 'complete'
    part, = report['parts']
    assert requests == ([(400, 600), (400, 364), (400, 364)] if rotate == 0
                        else [(600, 400), (364, 400), (364, 400)])
    assert [tile['box'] for tile in part['image_tiles']] == [[0, 0, 400, 364], [0, 236, 400, 600]]
    assert 'discarded' not in part['answer']
    assert part['usage']['total_tokens'] == 9
    assert part['truncated_responses'][0]['finish_reason'] == 'length'
    assert image.read_bytes() == original


@pytest.mark.parametrize('reason', ['content_filter', 'length'])
def test_unreadable_region_stays_failed_and_preserves_completed_regions(tmp_path, monkeypatch, reason):
    image = tmp_path / 'page.png'
    Image.new('RGB', (400, 600), 'red').save(image)
    requests = []

    def post(body, **kwargs):
        requests.append(body)
        finish = 'length' if len(requests) == 1 else 'stop' if len(requests) == 2 else reason
        return {'choices': [{'finish_reason': finish, 'message': {'content': 'region text'}}]}

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake')
    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(files=[image], output_dir=tmp_path / 'report')

    assert report['status'] == 'failed'
    assert report['parts'] == []
    assert report['error_code'] == 'attachment_response_incomplete'
    assert report['failed_response']['finish_reason'] == reason
    assert report['failed_part']['image_tiles'][0]['box'] == [0, 0, 400, 364]
    assert len(requests) <= 6


def test_region_fallback_keeps_original_coordinates_and_actual_crop_digests(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    Image.new('RGB', (600, 800), 'blue').save(image)
    requests = []

    def post(body, **kwargs):
        raw = base64.b64decode(body['messages'][1]['content'][1]['image_url']['url'].split(',', 1)[1])
        requests.append(raw)
        return {'choices': [{'finish_reason': 'length' if len(requests) == 1 else 'stop',
                             'message': {'content': 'visible region'}}]}

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake')
    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(files=[image], region=(100, 100, 500, 700),
                                   output_dir=tmp_path / 'report')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'image_region'
    part, = report['parts']
    assert part['region'] == [100, 100, 500, 700]
    assert [t['box'] for t in part['image_tiles']] == [[100, 100, 500, 464], [100, 336, 500, 700]]
    assert [t['png_sha256'] for t in part['image_tiles']] == [hashlib.sha256(raw).hexdigest() for raw in requests[1:]]


def test_fallback_uses_remaining_budget_and_does_not_send_after_exhaustion(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    Image.new('RGB', (400, 600), 'red').save(image)
    clock, timeouts = [0], []

    def post(body, *, timeout, **kwargs):
        timeouts.append(timeout)
        clock[0] += 6
        return {'choices': [{'finish_reason': 'length' if len(timeouts) == 1 else 'stop',
                             'message': {'content': 'visible region'}}]}

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake')
    monkeypatch.setattr(recognition.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(files=[image], budget_seconds=10, output_dir=tmp_path / 'report')

    assert timeouts == [10, 4]
    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_recognition_budget_exhausted'
    assert report['parts'] == []
    assert len(report['failed_part']['image_tiles']) == 1


def test_fallback_reuses_encoded_size_tiling_and_maps_children_to_source(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    Image.new('RGB', (400, 600), 'red').save(image)
    encode, requests = recognition._image_block, []

    def block(raw, **kwargs):
        with Image.open(BytesIO(raw)) as page:
            if page.size == (400, 364):
                raise ValueError('attachment_image_too_large')
        return encode(raw, **kwargs)

    def post(body, **kwargs):
        requests.append(body)
        return {'choices': [{'finish_reason': 'length' if len(requests) == 1 else 'stop',
                             'message': {'content': 'visible region'}}]}

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake')
    monkeypatch.setattr(recognition, '_image_block', block)
    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(files=[image], output_dir=tmp_path / 'report')

    assert report['status'] == 'complete'
    part, = report['parts']
    assert [t['box'] for t in part['image_tiles']] == [
        [0, 0, 264, 364], [136, 0, 400, 364],
        [0, 236, 264, 600], [136, 236, 400, 600]]
    assert len(requests) == 5
