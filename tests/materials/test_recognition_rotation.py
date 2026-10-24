from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
from random import Random

from PIL import Image
import pytest

from apps.oa.materials import recognize as attachment_recognition
from apps.oa.materials import cli as materials_cli


@pytest.fixture
def requests(monkeypatch, tmp_path):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-api-key')
    captured = []

    def post(body, **kwargs):
        captured.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'fixture answer'}}]}

    monkeypatch.setattr(attachment_recognition, '_post', post)
    return captured


def image_pixels(request):
    block = request['messages'][1]['content'][1]
    raw = base64.b64decode(block['image_url']['url'].split(',', 1)[1])
    with Image.open(BytesIO(raw)) as image:
        rgb = image.convert('RGB')
        return image.size, [rgb.getpixel((x, y)) for y in range(rgb.height) for x in range(rgb.width)]


@pytest.mark.parametrize(('rotate', 'size', 'values'), [
    (0, (3, 2), [1, 2, 3, 4, 5, 6]),
    (90, (2, 3), [4, 1, 5, 2, 6, 3]),
    (180, (3, 2), [6, 5, 4, 3, 2, 1]),
    (270, (2, 3), [3, 6, 2, 5, 1, 4]),
])
def test_recognize_rotates_reading_copy_clockwise(tmp_path, requests, rotate, size, values):
    source = tmp_path / 'page.png'
    image = Image.new('RGB', (3, 2))
    image.putdata([(value, 0, 0) for value in range(1, 7)])
    image.save(source)
    original = source.read_bytes()

    report = attachment_recognition.recognize(
        files=[source], rotate=rotate, output_dir=tmp_path / 'report')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'all_prepared_content'
    assert image_pixels(requests[0]) == (size, [(value, 0, 0) for value in values])
    assert source.read_bytes() == original
    part = report['parts'][0]
    assert part['rotate'] == rotate
    assert part['resource_id'] == 'file-1'
    assert part['prepared_resource_id'] == 'file-1'
    assert part['source_image_path'] == str(source)
    assert part['page_number'] == 1


def test_region_is_cropped_in_original_coordinates_before_rotation(tmp_path, requests):
    source = tmp_path / 'page.png'
    image = Image.new('RGB', (4, 3))
    image.putdata([(value, 0, 0) for value in range(1, 13)])
    image.save(source)
    original = source.read_bytes()

    report = attachment_recognition.recognize(
        files=[source], region=(1, 0, 4, 2), rotate=90, output_dir=tmp_path / 'report')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'image_region'
    part = report['parts'][0]
    assert part['region'] == [1, 0, 4, 2]
    assert part['source_image_size'] == [4, 3]
    assert part['image_tiles'][0]['box'] == [1, 0, 4, 2]
    assert part['rotate'] == 90
    assert image_pixels(requests[0]) == ((2, 3), [(value, 0, 0) for value in [6, 2, 7, 3, 8, 4]])
    assert source.read_bytes() == original
    question = requests[0]['messages'][1]['content'][0]['text']
    assert '顺时针旋转 90' in question
    assert '坐标均属于旋转前原图' in question


def test_cli_accepts_clockwise_rotation(tmp_path, requests, capsys):
    source = tmp_path / 'page.png'
    Image.new('RGB', (3, 2), (7, 0, 0)).save(source)

    code = materials_cli.main(['recognize', 
        '--file', str(source), '--rotate', '270', '--output-dir', str(tmp_path / 'report')])

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary['report_file']).read_text())
    assert summary['coverage'] == 'all_prepared_content'
    assert report['parts'][0]['rotate'] == 270
    assert image_pixels(requests[0])[0] == (2, 3)


def test_material_rotation_preserves_duplicate_attachment_identity_and_text(tmp_path, requests):
    from apps.oa.approvals.operations import Approval
    from tests.serial.test_approval_service import FakeOA

    source = tmp_path / 'page.png'
    Image.new('RGB', (3, 2), (7, 0, 0)).save(source)
    original = source.read_bytes()
    material = {'request_id': '101', 'workflow_key': 'travel_expense',
                'resources': [{'resource_id': rid, 'name': rid + '.zip'} for rid in ('a', 'b')],
                'prepared': {'images': [
                    {'resource_id': rid + ':invoice.pdf', 'parent_resource_id': rid,
                     'path': str(source), 'content_sha256': hashlib.sha256(original).hexdigest()}
                    for rid in ('a', 'b')],
                    'manifest': [], 'structured_contents': [
                        {'resource_id': 'a:invoice.pdf', 'parent_resource_id': 'a', 'text': '原件 25.00'}]}}
    app = Approval(tmp_path / 'approval', api=FakeOA(), configs=[], collector=lambda *args, **kwargs: material)
    task = app.collect(app.pending()['run_id'], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    material_file = Path(task['material_file'])
    frozen = material_file.read_bytes()
    baseline = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'baseline')
    requests.clear()

    report = attachment_recognition.recognize(
        material_file=material_file, rotate=90, output_dir=tmp_path / 'rotated')

    assert report['status'] == 'complete'
    assert report['coverage'] == baseline['coverage'] == 'all_prepared_content'
    images = [part for part in report['parts'] if part['kind'] == 'image']
    assert [(p['resource_id'], p['prepared_resource_id'], p['page_number'], p['rotate']) for p in images] == [
        ('a', 'a:invoice.pdf', 1, 90), ('b', 'b:invoice.pdf', 1, 90)]
    assert all(p['source_image_path'] == str(source) for p in images)
    assert images[1]['reused_from'] == 'image-1'
    image_requests = [r for r in requests if r['messages'][1]['content'][1]['type'] == 'image_url']
    assert len(image_requests) == 1
    assert image_pixels(image_requests[0])[0] == (2, 3)
    texts = [p for p in report['parts'] if p['kind'] == 'text']
    assert texts == [p for p in baseline['parts'] if p['kind'] == 'text']
    assert 'rotate' not in texts[0]
    assert source.read_bytes() == original
    assert material_file.read_bytes() == frozen

    assert app.submit(task['task_id'], opinion='离线测试意见', recognition=report)['status'] == 'preview'
    details = app.inspect(task['task_id'])
    saved = [json.loads(row['metadata_json']) for row in details['contents'] if row['source'] == 'deepseek']
    saved_images = [p for p in saved if p.get('kind') == 'image']
    assert [(p['resource_id'], p['prepared_resource_id'], p['rotate']) for p in saved_images] == [
        ('a', 'a:invoice.pdf', 90), ('b', 'b:invoice.pdf', 90)]
    assert details['submissions'] == []


@pytest.fixture(params=[('GIF', '.gif'), ('WEBP', '.webp'), ('GIF', '.png'), ('WEBP', '.png')])
def multiframe_source(tmp_path, request):
    image_format, suffix = request.param
    source = tmp_path / ('animated' + suffix)
    frames = [Image.new('RGB', (8, 6), color) for color in ('red', 'blue')]
    frames[0].save(source, format=image_format, save_all=True, append_images=frames[1:], duration=100, loop=0)
    with Image.open(source) as image:
        assert image.n_frames == 2
    return source


@pytest.mark.parametrize('rotate', [90, 180, 270])
def test_cli_rejects_multiframe_rotation_and_keeps_default_reading(
        tmp_path, requests, capsys, multiframe_source, rotate):
    original = multiframe_source.read_bytes()
    baseline = attachment_recognition.recognize(files=[multiframe_source], output_dir=tmp_path / 'baseline')
    assert baseline['status'] == 'complete'
    url = requests[0]['messages'][1]['content'][1]['image_url']['url']
    assert base64.b64decode(url.split(',', 1)[1]) == original
    requests.clear()

    code = materials_cli.main(['recognize', 
        '--file', str(multiframe_source), '--rotate', str(rotate), '--output-dir', str(tmp_path / 'report')])

    assert code == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary['status'] == 'failed'
    assert summary['error_code'] == 'attachment_multiframe_rotation_unsupported'
    report = json.loads(Path(summary['report_file']).read_text())
    assert report['status'] == 'failed'
    assert report['parts'] == []
    assert requests == []
    assert multiframe_source.read_bytes() == original


@pytest.mark.parametrize('rotate', [0, 90, 180, 270])
def test_region_reports_multiframe_rotation_limit_without_losing_frames(
        tmp_path, requests, multiframe_source, rotate):
    original = multiframe_source.read_bytes()

    report = attachment_recognition.recognize(
        files=[multiframe_source], region=(1, 1, 7, 5), rotate=rotate, output_dir=tmp_path / 'report')

    assert report['status'] == 'failed'
    assert report['error_code'] == ('attachment_multiframe_rotation_unsupported' if rotate
                                    else 'attachment_region_source_invalid')
    assert report['coverage'] == 'image_region'
    assert report['failed_part']['region'] == [1, 1, 7, 5]
    assert report['failed_part']['source_image_size'] == [8, 6]
    assert report['parts'] == []
    assert requests == []
    assert multiframe_source.read_bytes() == original


@pytest.mark.parametrize('rotate', [-90, 45, 360, '90', 90.0, True, None])
def test_api_rejects_unsupported_rotation_before_creating_report(tmp_path, requests, rotate):
    output = tmp_path / 'report'
    with pytest.raises(ValueError, match='attachment_rotation_invalid'):
        attachment_recognition.recognize(files=[tmp_path / 'page.png'], rotate=rotate, output_dir=output)
    assert not output.exists()
    assert requests == []


def test_rotated_large_image_keeps_tile_boxes_in_original_coordinates(tmp_path, requests):
    source = tmp_path / 'wide.png'
    image = Image.new('RGB', (8193, 2), (1, 0, 0))
    image.paste((2, 0, 0), (0, 1, 8193, 2))
    image.save(source)

    report = attachment_recognition.recognize(
        files=[source], rotate=90, output_dir=tmp_path / 'report')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'all_prepared_content'
    assert [tile['box'] for tile in report['parts'][0]['image_tiles']] == [
        [0, 0, 4160, 2], [4032, 0, 8193, 2]]
    assert [image_pixels(r)[0] for r in requests] == [(2, 4160), (2, 4161)]
    assert image_pixels(requests[0])[1][:2] == [(2, 0, 0), (1, 0, 0)]


@pytest.fixture
def jpeg_source(tmp_path):
    source = tmp_path / 'page.jpg'
    image = Image.frombytes('RGB', (96, 64), Random(0).randbytes(96 * 64 * 3))
    image.save(source, quality=30)
    return source


def assert_rotated_tiles(report, requests, source, byte_limit):
    part = report['parts'][0]
    tiles = part['image_tiles']
    assert len(tiles) == len(requests) > 1
    covered = set()
    with Image.open(source) as original:
        for tile, request in zip(tiles, requests):
            x0, y0, x1, y1 = tile['box']
            assert 0 <= x0 < x1 <= original.width
            assert 0 <= y0 < y1 <= original.height
            url = request['messages'][1]['content'][1]['image_url']['url']
            assert len(base64.b64decode(url.split(',', 1)[1])) <= byte_limit
            assert image_pixels(request) == ((y1 - y0, x1 - x0), [
                original.getpixel((x, y)) for x in range(x0, x1) for y in range(y1 - 1, y0 - 1, -1)])
            covered.update((x, y) for x in range(x0, x1) for y in range(y0, y1))
        assert covered == {(x, y) for x in range(original.width) for y in range(original.height)}


def test_rotated_jpeg_splits_when_png_exceeds_byte_limit(tmp_path, requests, monkeypatch, jpeg_source):
    original = jpeg_source.read_bytes()
    byte_limit = 5000
    assert len(original) < byte_limit
    with Image.open(jpeg_source) as image:
        assert max(image.size) < attachment_recognition.MAX_IMAGE_DIMENSION
        png = BytesIO()
        image.transpose(Image.Transpose.ROTATE_270).save(png, format='PNG')
        assert len(png.getvalue()) > byte_limit
    monkeypatch.setattr(attachment_recognition, 'MAX_FILE_BYTES', byte_limit)
    baseline = attachment_recognition.recognize(files=[jpeg_source], output_dir=tmp_path / 'baseline')
    assert baseline['status'] == 'complete'
    requests.clear()

    report = attachment_recognition.recognize(
        files=[jpeg_source], rotate=90, output_dir=tmp_path / 'report')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'all_prepared_content'
    assert_rotated_tiles(report, requests, jpeg_source, byte_limit)
    assert jpeg_source.read_bytes() == original


def test_material_tiles_split_again_when_rotation_increases_encoded_bytes(
        tmp_path, requests, monkeypatch, jpeg_source):
    from apps.oa.approvals.operations import Approval
    from tests.serial.test_approval_service import FakeOA

    original = jpeg_source.read_bytes()
    with Image.open(jpeg_source) as image, image.crop((0, 0, 72, 64)) as tile:
        before, after = BytesIO(), BytesIO()
        tile.save(before, format='PNG')
        tile.transpose(Image.Transpose.ROTATE_270).save(after, format='PNG')
        byte_limit = len(before.getvalue())
        assert byte_limit < len(after.getvalue())
    material = {'request_id': '101', 'workflow_key': 'travel_expense',
                'resources': [{'resource_id': 'archive', 'name': 'invoices.zip'}],
                'prepared': {'images': [{
                    'resource_id': 'archive:page.jpg', 'parent_resource_id': 'archive',
                    'path': str(jpeg_source), 'content_sha256': hashlib.sha256(original).hexdigest()}],
                    'manifest': [], 'structured_contents': []}}
    app = Approval(tmp_path / 'approval', api=FakeOA(), configs=[], collector=lambda *args, **kwargs: material)
    task = app.collect(app.pending()['run_id'], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    material_file = Path(task['material_file'])
    frozen = material_file.read_bytes()
    monkeypatch.setattr(attachment_recognition, 'MAX_FILE_BYTES', byte_limit)
    monkeypatch.setattr(attachment_recognition, 'MAX_IMAGE_DIMENSION', 80)

    report = attachment_recognition.recognize(
        material_file=material_file, rotate=90, output_dir=tmp_path / 'report')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'all_prepared_content'
    assert_rotated_tiles(report, requests, jpeg_source, byte_limit)
    assert jpeg_source.read_bytes() == original
    assert material_file.read_bytes() == frozen
    assert app.submit(task['task_id'], opinion='离线测试意见', recognition=report)['status'] == 'preview'
    details = app.inspect(task['task_id'])
    saved = [json.loads(row['metadata_json']) for row in details['contents'] if row['source'] == 'deepseek']
    assert [(p['resource_id'], p['prepared_resource_id'], p['rotate']) for p in saved] == [
        ('archive', 'archive:page.jpg', 90)]
    assert saved[0]['image_tiles'] == report['parts'][0]['image_tiles']
    assert details['submissions'] == []


def test_resource_selection_keeps_original_parts_and_parser_content(tmp_path, requests, selected_material):
    material_file, _, _ = selected_material
    baseline = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'baseline')
    requests.clear()

    report = attachment_recognition.recognize(
        material_file=material_file, resource_id='b', output_dir=tmp_path / 'selected')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'selected_resource'
    assert report['selection'] == {'resource_id': 'b'}
    expected = [part for part in baseline['parts'] if part['resource_id'] == 'b']
    for part, original in zip(report['parts'], expected):
        assert {key: part[key] for key in ('part_id', 'resource_id', 'prepared_resource_id', 'evidence_ref', 'source', 'answer')} == {
            key: original[key] for key in ('part_id', 'resource_id', 'prepared_resource_id', 'evidence_ref', 'source', 'answer')}
    assert [part['part_id'] for part in report['parts']] == ['image-2', 'image-3', 'text-2']
    assert [part['page_number'] for part in report['parts'] if part['kind'] == 'image'] == [1, 2]
    assert len(requests) == 1


@pytest.fixture
def selected_material(tmp_path):
    source = tmp_path / 'page.png'
    image = Image.new('RGB', (4, 3))
    image.putdata([(value, 0, 0) for value in range(1, 13)])
    image.save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    material = {'request_id': '101', 'workflow_key': 'travel_expense',
                'resources': [{'resource_id': rid, 'name': rid + '.zip'} for rid in ('a', 'b')],
                'prepared': {'images': [
                    {'resource_id': rid + ':invoice.pdf', 'parent_resource_id': rid,
                     'path': str(source), 'content_sha256': digest} for rid in ('a', 'b', 'b')],
                    'manifest': [], 'structured_contents': [
                        {'resource_id': rid + ':invoice.pdf', 'parent_resource_id': rid, 'text': rid}
                        for rid in ('a', 'b')]}}
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    return material_file, material, source


def test_material_navigation_matches_recognition_page_identity(tmp_path, requests, selected_material):
    from apps.oa.materials.read import read

    material_file, material, source = selected_material
    material['prepared']['images'].append({
        **material['prepared']['images'][0], 'resource_id': 'a:second.pdf', 'page_number': 99})
    material_file.write_text(json.dumps(material))
    frozen = material_file.read_bytes()
    original = source.read_bytes()

    navigation = json.loads(read(material_file).split('[reader.image_tool_routes]\n')[1])
    report = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')

    assert report['status'] == 'complete'
    assert navigation['coverage'] == 'navigation_only'
    expected = [
        ('a', 'a:invoice.pdf', 1, 'material:prepared.images.a.1'),
        ('b', 'b:invoice.pdf', 1, 'material:prepared.images.b.1'),
        ('b', 'b:invoice.pdf', 2, 'material:prepared.images.b.2'),
        ('a', 'a:second.pdf', 2, 'material:prepared.images.a.2'),
    ]
    images = [part for part in report['parts'] if part['kind'] == 'image']
    assert [(part['resource_id'], part['prepared_resource_id'], part['page_number'], part['evidence_ref'])
            for part in images] == expected
    assert [route['evidence_ref'] for route in navigation['pages']] == [item[3] for item in expected]
    assert [route['material_pointer'] for route in navigation['pages']] == [
        'material.prepared.images[0]', 'material.prepared.images[1]',
        'material.prepared.images[2]', 'material.prepared.images[3]']
    assert [route['resource_pointer'] for route in navigation['pages']] == [
        'material.resources[0]', 'material.resources[1]', 'material.resources[1]', 'material.resources[0]']
    assert [route['source'] for route in navigation['pages']] == material['prepared']['images']
    assert all('region_read' in route for route in navigation['pages'])
    assert [(part['resource_id'], part['source'], part['evidence_ref'])
            for part in report['parts'] if part['kind'] == 'text'] == [
        ('a', 'parser', 'material:prepared.structured_contents[0]'),
        ('b', 'parser', 'material:prepared.structured_contents[1]')]
    assert [part.get('reused_from') for part in images] == [None, 'image-1', 'image-1', 'image-1']
    assert len(requests) == 1
    assert material_file.read_bytes() == frozen
    assert source.read_bytes() == original


@pytest.mark.parametrize(('resource_id', 'status', 'error'), [
    ('', 'failed', 'attachment_page_unbound'),
    ('orphan', 'complete', None),
])
def test_material_navigation_keeps_missing_resource_guards_separate_from_recognition(
        tmp_path, requests, selected_material, resource_id, status, error):
    from apps.oa.materials.read import read

    material_file, material, _ = selected_material
    material['prepared']['images'] = [{
        **material['prepared']['images'][0], 'resource_id': resource_id, 'parent_resource_id': ''}]
    material['prepared']['structured_contents'] = []
    material_file.write_text(json.dumps(material))

    navigation = json.loads(read(material_file).split('[reader.image_tool_routes]\n')[1])
    report = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')

    route = navigation['pages'][0]
    assert route['resource_pointer'] is None
    assert 'region_read' not in route
    assert 'auxiliary_copy' not in route
    assert report['status'] == status
    assert report.get('error_code') == error
    if error:
        assert report['parts'] == requests == []
    else:
        assert report['parts'][0]['evidence_ref'] == route['evidence_ref']
        assert report['parts'][0]['resource_id'] == 'orphan'


def test_page_selection_reads_only_original_image_page(tmp_path, requests, selected_material):
    material_file, material, source = selected_material
    material['prepared']['images'][0]['path'] = str(tmp_path / 'unselected-a.png')
    material['prepared']['images'][1]['path'] = str(tmp_path / 'unselected-b.png')
    material_file.write_text(json.dumps(material))

    report = attachment_recognition.recognize(
        material_file=material_file, resource_id='b', page=2, output_dir=tmp_path / 'selected')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'selected_page'
    assert report['selection'] == {'resource_id': 'b', 'page': 2}
    assert len(report['parts']) == len(requests) == 1
    part = report['parts'][0]
    assert (part['part_id'], part['evidence_ref'], part['prepared_resource_id'], part['page_number']) == (
        'image-3', 'material:prepared.images.b.2', 'b:invoice.pdf', 2)
    assert image_pixels(requests[0])[0] == (4, 3)


def test_material_region_crops_selected_page_before_rotation_and_keeps_prior_report(
        tmp_path, requests, selected_material):
    material_file, _, source = selected_material
    original, frozen = source.read_bytes(), material_file.read_bytes()
    baseline = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'baseline')
    prior_report = Path(baseline['report_file']).read_bytes()
    requests.clear()

    report = attachment_recognition.recognize(
        material_file=material_file, resource_id='b', page=2, region=(1, 0, 4, 2), rotate=90,
        output_dir=tmp_path / 'selected')

    assert report['status'] == 'complete'
    assert report['coverage'] == 'image_region'
    assert report['selection'] == {'resource_id': 'b', 'page': 2}
    assert len(report['parts']) == len(requests) == 1
    part = report['parts'][0]
    assert (part['part_id'], part['evidence_ref'], part['page_number'], part['rotate']) == (
        'image-3', 'material:prepared.images.b.2', 2, 90)
    assert part['region'] == [1, 0, 4, 2]
    assert image_pixels(requests[0]) == ((2, 3), [(value, 0, 0) for value in [6, 2, 7, 3, 8, 4]])
    assert source.read_bytes() == original
    assert material_file.read_bytes() == frozen
    assert Path(baseline['report_file']).read_bytes() == prior_report


def test_selected_observations_keep_sources_and_bind_to_real_attachments(
        tmp_path, requests, selected_material):
    from apps.oa.approvals.operations import Approval
    from tests.serial.test_approval_service import FakeOA

    _, material, _ = selected_material
    api = FakeOA()
    app = Approval(tmp_path / 'approval', api=api, configs=[], collector=lambda *args, **kwargs: material)
    task = app.collect(app.pending()['run_id'], agent_id='agent-a', expected_task_id=1, agent_workflow_key='travel_expense')
    report = attachment_recognition.recognize(
        material_file=Path(task['material_file']), resource_id='b', prompt='读取原始金额',
        output_dir=tmp_path / 'selected')

    observations = json.loads(Path(report['observations_file']).read_text())
    assert [item['source'] for item in observations] == ['deepseek', 'deepseek', 'parser']
    assert [item['resource_id'] for item in observations] == ['b', 'b', 'b']
    for observation, part in zip(observations, report['parts']):
        assert observation['content'] == part['answer']
        assert observation['evidence_ref'] == part['evidence_ref']
        assert observation['prepared_resource_id'] == 'b:invoice.pdf'
        assert observation['report_file'] == report['report_file']
        assert observation['prompt'] == '读取原始金额'
        assert observation['coverage'] == 'selected_resource'
    assert json.loads(Path(report['report_file']).read_text()) == report
    assert app.submit(task['task_id'], opinion='离线测试意见', observations=observations)['status'] == 'preview'
    details = app.inspect(task['task_id'])
    attachment_ids = {item['resource_id']: item['id'] for item in details['attachments']}
    saved = [item for item in details['contents'] if json.loads(item['metadata_json']).get('report_file') == report['report_file']]
    assert [(item['attachment_id'], item['source'], item['content']) for item in saved] == [
        (attachment_ids['b'], observation['source'], observation['content']) for observation in observations]
    assert api.posts == details['submissions'] == []


def test_cli_returns_selected_page_observations(tmp_path, requests, selected_material, capsys):
    material_file, _, _ = selected_material

    code = materials_cli.main(['recognize', 
        '--material-file', str(material_file), '--resource-id', 'b', '--page', '2',
        '--region', '1', '0', '4', '2', '--rotate', '90', '--output-dir', str(tmp_path / 'selected')])

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['coverage'] == 'image_region'
    assert summary['selection'] == {'resource_id': 'b', 'page': 2}
    observations = json.loads(Path(summary['observations_file']).read_text())
    assert len(observations) == len(requests) == 1
    assert observations[0]['evidence_ref'] == 'material:prepared.images.b.2'
    assert observations[0]['region'] == [1, 0, 4, 2]
    assert observations[0]['rotate'] == 90


@pytest.mark.parametrize(('resource_id', 'page', 'error', 'coverage'), [
    ('missing', None, 'attachment_resource_not_found', 'selected_resource'),
    ('b:invoice.pdf', None, 'attachment_resource_not_found', 'selected_resource'),
    ('b', 3, 'attachment_page_not_found', 'selected_page'),
])
def test_missing_selection_reports_target_without_reading_other_parts(
        tmp_path, requests, selected_material, resource_id, page, error, coverage):
    material_file, _, _ = selected_material

    report = attachment_recognition.recognize(
        material_file=material_file, resource_id=resource_id, page=page, output_dir=tmp_path / 'selected')

    assert report['status'] == 'failed'
    assert report['error_code'] == error
    assert report['selection']['resource_id'] == resource_id
    assert report['coverage'] == coverage
    assert report['parts'] == requests == []
    assert json.loads(Path(report['observations_file']).read_text()) == []


@pytest.mark.parametrize(('resource_id', 'expected_status'), [('a', 'complete'), ('b', 'failed')])
def test_material_region_requires_one_image_and_excludes_parser_text(
        tmp_path, requests, selected_material, resource_id, expected_status):
    material_file, _, _ = selected_material

    report = attachment_recognition.recognize(
        material_file=material_file, resource_id=resource_id, region=(1, 0, 4, 2),
        output_dir=tmp_path / 'selected')

    assert report['status'] == expected_status
    if expected_status == 'complete':
        assert len(report['parts']) == len(requests) == 1
        assert report['parts'][0]['kind'] == 'image'
        assert report['parts'][0]['resource_id'] == 'a'
    else:
        assert report['error_code'] == 'attachment_region_source_invalid'
        assert report['parts'] == requests == []


def test_selected_parser_needs_no_model_key_when_other_resources_have_images(
        tmp_path, requests, selected_material, monkeypatch):
    material_file, material, _ = selected_material
    material['prepared']['images'] = [part for part in material['prepared']['images'] if part['parent_resource_id'] == 'a']
    material_file.write_text(json.dumps(material))
    monkeypatch.setenv('OA_REVIEW_API_KEY', '')

    report = attachment_recognition.recognize(
        material_file=material_file, resource_id='b', output_dir=tmp_path / 'selected')

    assert report['status'] == 'complete'
    assert [part['source'] for part in report['parts']] == ['parser']
    assert report['parts'][0]['part_id'] == 'text-2'
    assert json.loads(report['parts'][0]['answer'])['text'] == 'b'
    assert requests == []


def test_region_and_rotation_observations_have_distinct_locators(
        tmp_path, requests, selected_material):
    material_file, _, _ = selected_material
    observations = []
    for index, (region, rotate) in enumerate([((0, 0, 2, 2), 0), ((1, 0, 4, 2), 0), ((1, 0, 4, 2), 90)]):
        report = attachment_recognition.recognize(
            material_file=material_file, resource_id='b', page=2, region=region, rotate=rotate,
            output_dir=tmp_path / f'selected-{index}')
        assert report['status'] == 'complete'
        observations.extend(json.loads(Path(report['observations_file']).read_text()))

    assert len({item['locator'] for item in observations}) == 3
    assert {item['evidence_ref'] for item in observations} == {'material:prepared.images.b.2'}
    assert {item['source'] for item in observations} == {'deepseek'}


@pytest.mark.parametrize(('arguments', 'error'), [
    ({'resource_id': ''}, 'attachment_resource_invalid'),
    ({'page': 1}, 'attachment_page_invalid'),
    ({'resource_id': 'b', 'page': 0}, 'attachment_page_invalid'),
    ({'resource_id': 'b', 'page': True}, 'attachment_page_invalid'),
    ({'region': (0, 0, 1, 1)}, 'attachment_region_source_invalid'),
])
def test_invalid_material_selection_stops_before_report(
        tmp_path, requests, selected_material, arguments, error):
    material_file, _, _ = selected_material
    output = tmp_path / 'selected'
    with pytest.raises(ValueError, match=error):
        attachment_recognition.recognize(material_file=material_file, output_dir=output, **arguments)
    assert not output.exists()
    assert requests == []


def test_file_selection_is_rejected_before_reading_source(tmp_path, requests):
    output = tmp_path / 'selected'
    with pytest.raises(ValueError, match='attachment_resource_invalid'):
        attachment_recognition.recognize(files=[tmp_path / 'missing.png'], resource_id='file-1', output_dir=output)
    assert not output.exists()
    assert requests == []


@pytest.mark.parametrize('changed', ['material', 'image'])
def test_changed_selected_input_keeps_failed_observations_empty(
        tmp_path, requests, selected_material, monkeypatch, changed):
    material_file, _, source = selected_material

    def post(*args, **kwargs):
        if changed == 'material':
            material_file.write_text(material_file.read_text() + '\n')
        else:
            Image.new('RGB', (4, 3), 'red').save(source)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'fixture answer'}}]}

    monkeypatch.setattr(attachment_recognition, '_post', post)
    report = attachment_recognition.recognize(
        material_file=material_file, resource_id='b', page=2, output_dir=tmp_path / 'selected')

    assert report['status'] == 'failed'
    assert report['error_code'] == ('frozen_material_changed' if changed == 'material' else 'attachment_page_changed')
    assert json.loads(Path(report['observations_file']).read_text()) == []
