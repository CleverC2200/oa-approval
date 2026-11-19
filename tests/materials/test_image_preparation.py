from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
from random import Random
import struct

from PIL import Image
import pytest

from apps.oa.materials.prepare import prepare
from apps.oa.materials import recognize as recognition


@pytest.fixture
def requests(monkeypatch, tmp_path):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-api-key')
    captured = []

    def post(body, **kwargs):
        captured.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'visible fixture text'}}]}

    monkeypatch.setattr(recognition, '_post', post)
    return captured


def request_bytes(request):
    url = request['messages'][1]['content'][1]['image_url']['url']
    return base64.b64decode(url.split(',', 1)[1])


@pytest.mark.parametrize('frame_layout', ['normal', 'overlap', 'gap'])
def test_prepare_recognize_mpo_keeps_original_jpeg_frames_and_source_ownership(tmp_path, requests, frame_layout):
    source = tmp_path / 'photos.jpg'
    with Image.frombytes('RGB', (96, 64), Random(0).randbytes(96 * 64 * 3)) as first:
        with Image.new('RGB', (48, 32), 'blue') as second:
            first.save(source, format='MPO', save_all=True, append_images=[second], quality=90)
    if frame_layout != 'normal':
        with Image.open(source) as image:
            primary_size = image.mpinfo[0xB002][0]['Size']
            secondary = image.mpinfo[0xB002][1]
        original = source.read_bytes()
        gap = b'junk\xff\xd9' if frame_layout == 'gap' else b''
        if gap:
            entry = struct.pack('<LLLHH', 0, secondary['Size'], secondary['DataOffset'], 0, 0)
            assert original.count(entry) == 1
            original = original.replace(entry, struct.pack('<LLLHH', 0, secondary['Size'],
                                                           secondary['DataOffset'] + len(gap), 0, 0))
            original = original[:primary_size] + gap + original[primary_size:]
        entry = struct.pack('<LLLHH', 0x030000, primary_size, 0, 0, 0)
        assert original.count(entry) == 1
        source.write_bytes(original.replace(entry, struct.pack('<LLLHH', 0x030000,
                                                               primary_size + len(gap) + 32, 0, 0, 0)))
    original = source.read_bytes()
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    if frame_layout == 'gap':
        assert result['status'] == 'failed'
        assert result['error_code'] == 'attachment_mpo_frame_invalid'
        assert source.read_bytes() == original
        return
    assert result['status'] == 'complete'
    material_file = Path(result['material_file'])
    frozen = material_file.read_bytes()
    material = json.loads(frozen)

    report = recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'all_prepared_content'
    pages = material['prepared']['images']
    assert len(pages) == len(requests) == len(report['parts']) == 2
    assert all(Path(page['path']).suffix == '.jpg' for page in pages)
    with Image.open(source) as source_image:
        for index, (page, request, part) in enumerate(zip(pages, requests, report['parts'])):
            source_image.seek(index)
            with Image.open(Path(page['path'])) as prepared_image:
                assert prepared_image.format == 'JPEG'
                assert getattr(prepared_image, 'n_frames', 1) == 1
                assert prepared_image.size == source_image.size
                assert prepared_image.tobytes() == source_image.tobytes()
            assert len(request_bytes(request)) < len(original)
            assert request_bytes(request) == Path(page['path']).read_bytes()
            assert page['source_path'] == str(source)
            assert page['source_resource_id'] == part['resource_id'] == part['prepared_resource_id'] == 'local-1'
            assert part['page_number'] == index + 1
    assert source.read_bytes() == original
    assert material_file.read_bytes() == frozen


def test_prepare_recognize_splits_source_larger_than_each_model_request(tmp_path, requests, monkeypatch):
    source = tmp_path / 'page.png'
    with Image.frombytes('RGB', (96, 64), Random(0).randbytes(96 * 64 * 3)) as image:
        image.save(source)
    original = source.read_bytes()
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'complete'
    material_file = Path(result['material_file'])
    frozen = material_file.read_bytes()
    byte_limit = 5000
    assert len(original) > byte_limit
    monkeypatch.setattr(recognition, 'MAX_FILE_BYTES', byte_limit)
    monkeypatch.setattr(recognition, 'MAX_SOURCE_BYTES', len(original) + len(frozen))

    report = recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'all_prepared_content'
    part = report['parts'][0]
    assert part['resource_id'] == part['prepared_resource_id'] == 'local-1'
    assert part['source_image_size'] == [96, 64]
    assert len(part['image_tiles']) == len(requests) > 1
    covered = set()
    with Image.open(source) as image:
        for tile, request in zip(part['image_tiles'], requests):
            raw = request_bytes(request)
            assert len(raw) <= byte_limit
            with Image.open(BytesIO(raw)) as reading, image.crop(tile['box']) as crop:
                assert reading.size == crop.size
                assert reading.tobytes() == crop.tobytes()
            x0, y0, x1, y1 = tile['box']
            covered.update((x, y) for x in range(x0, x1) for y in range(y0, y1))
    assert covered == {(x, y) for x in range(96) for y in range(64)}
    assert source.read_bytes() == original
    assert material_file.read_bytes() == frozen


@pytest.mark.parametrize(('budget', 'suffix', 'error'), [
    ('bytes', 'png', 'attachment_file_too_large'),
    ('pixels', 'png', 'attachment_image_large_decode_unsupported'),
    ('decoded_memory', 'jpg', 'attachment_image_decoded_memory_limit'),
])
def test_prepare_recognize_keeps_source_and_decode_budgets(tmp_path, requests, monkeypatch, budget, suffix, error):
    source = tmp_path / f'page.{suffix}'
    with Image.frombytes('RGB', (96, 64), Random(0).randbytes(96 * 64 * 3)) as image:
        image.save(source)
    original = source.read_bytes()
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'complete'
    material_file = Path(result['material_file'])
    frozen = material_file.read_bytes()
    if budget == 'bytes':
        assert len(original) > len(frozen)
        monkeypatch.setattr(recognition, 'MAX_SOURCE_BYTES', len(original) - 1)
    else:
        monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 1000)
        monkeypatch.setattr(recognition, 'MAX_DECODED_IMAGE_BYTES', 10000)

    report = recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')

    assert report['status'] == 'failed'
    assert report['error_code'] == error
    assert requests == []
    assert source.read_bytes() == original
    assert material_file.read_bytes() == frozen


def test_prepare_recognize_rereads_large_source_after_truncated_tile(tmp_path, requests, monkeypatch):
    source = tmp_path / 'page.png'
    with Image.frombytes('RGB', (320, 288), Random(0).randbytes(320 * 288 * 3)) as image:
        image.save(source)
    original = source.read_bytes()
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'complete'
    material_file = Path(result['material_file'])
    frozen = material_file.read_bytes()
    byte_limit = 240000
    assert len(original) > byte_limit
    monkeypatch.setattr(recognition, 'MAX_FILE_BYTES', byte_limit)

    def post(body, **kwargs):
        requests.append(body)
        return {'choices': [{'finish_reason': 'length' if len(requests) == 1 else 'stop',
                             'message': {'content': 'discarded' if len(requests) == 1 else 'complete tile'}}]}

    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')

    assert report['status'] == 'complete'
    part = report['parts'][0]
    assert part['truncated_responses'][0]['finish_reason'] == 'length'
    assert 'discarded' not in part['answer']
    assert len(part['image_tiles']) == len(requests) - 1
    assert all(len(request_bytes(request)) <= byte_limit for request in requests)
    assert source.read_bytes() == original
    assert material_file.read_bytes() == frozen
