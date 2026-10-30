import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import struct
import zlib

from PIL import Image, PngImagePlugin
import pytest

from apps.oa.materials import cli as materials_cli
from apps.oa.materials import recognize as recognition
from tests.materials.test_recognition_rotation import requests


def png_bytes(method=b'0'):
    info = PngImagePlugin.PngInfo()
    info.add(b'zTXt', b'EINVOICE\0' + method + zlib.compress(b'<EInvoice>synthetic</EInvoice>'))
    with Image.new('P', (3, 2)) as image:
        image.putpalette([value for i in range(256) for value in (i, 0, 255 - i)])
        image.putdata([0, 1, 2, 3, 4, 5])
        image.info['transparency'] = bytes(range(256))
        output = BytesIO()
        image.save(output, format='PNG', pnginfo=info)
    return output.getvalue()


def request_bytes(request):
    url = request['messages'][1]['content'][1]['image_url']['url']
    return base64.b64decode(url.split(',', 1)[1])


@pytest.mark.parametrize('method', [b'0', b'\0'])
@pytest.mark.parametrize('mode', ['full', 'region', 'rotate', 'tiles'])
def test_material_cli_reads_png_without_changing_original_or_resource_identity(
        tmp_path, requests, monkeypatch, capsys, method, mode):
    source = tmp_path / 'invoice.png'
    original = png_bytes(method)
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    material = tmp_path / 'material.json'
    material.write_text(json.dumps({'request_id': 'synthetic', 'prepared': {
        'images': [{'resource_id': rid, 'path': str(source), 'content_sha256': digest}
                   for rid in ('invoice-a', 'invoice-b')],
        'manifest': [], 'structured_contents': []}}))
    frozen = material.read_bytes()
    args = ['recognize', '--material-file', str(material), '--output-dir', str(tmp_path / 'report')]
    if mode == 'region':
        args += ['--resource-id', 'invoice-a', '--page', '1', '--region', '1', '0', '3', '2']
    if mode == 'rotate':
        args += ['--rotate', '90']
    if mode == 'tiles':
        monkeypatch.setattr(recognition, 'MAX_IMAGE_DIMENSION', 2)

    assert materials_cli.main(args) == 0

    summary = json.loads(capsys.readouterr().out)
    report = json.loads(Path(summary['report_file']).read_text())
    assert report['status'] == 'complete'
    assert report['coverage'] == ('image_region' if mode == 'region' else 'all_prepared_content')
    expected_ids = ['invoice-a'] if mode == 'region' else ['invoice-a', 'invoice-b']
    assert [p['resource_id'] for p in report['parts']] == expected_ids
    assert [p['prepared_resource_id'] for p in report['parts']] == expected_ids
    assert all(p['content_sha256'] == digest and p['source_image_path'] == str(source)
               and p['page_number'] == 1 for p in report['parts'])
    if mode != 'region':
        assert report['parts'][1]['reused_from'] == 'image-1'
    with Image.open(BytesIO(png_bytes(b'\0'))) as expected:
        if mode == 'region':
            expected = expected.crop((1, 0, 3, 2))
        if mode == 'rotate':
            expected = expected.transpose(Image.Transpose.ROTATE_270)
        boxes = report['parts'][0].get('image_tiles', [])
        for index, request in enumerate(requests):
            with Image.open(BytesIO(request_bytes(request))) as actual:
                reference = expected.crop(boxes[index]['box']) if mode == 'tiles' else expected
                assert actual.size == reference.size
                assert actual.convert('RGBA').tobytes() == reference.convert('RGBA').tobytes()
    if method == b'0':
        repair = report['parts'][0]['image_normalization']
        assert repair['kind'] == 'png_ztxt_compression_method'
        assert repair['source_sha256'] == digest
        if mode == 'full':
            assert repair['reading_copy_sha256'] == hashlib.sha256(request_bytes(requests[0])).hexdigest()
            assert request_bytes(requests[0]) == png_bytes(b'\0')
    else:
        assert all('image_normalization' not in p for p in report['parts'])
        if mode == 'full':
            assert request_bytes(requests[0]) == original
    assert source.read_bytes() == original
    assert material.read_bytes() == frozen


@pytest.mark.parametrize('damage', ['crc', 'truncated', 'method', 'pixels', 'text_limit'])
def test_png_compatibility_does_not_hide_other_corruption(tmp_path, requests, damage):
    original = bytearray(png_bytes(b'1' if damage == 'method' else b'0'))
    offset = 8
    while offset + 12 <= len(original):
        length = int.from_bytes(original[offset:offset + 4], 'big')
        kind = original[offset + 4:offset + 8]
        end = offset + length + 12
        if kind == b'zTXt' and damage == 'crc':
            original[end - 1] ^= 1
        if kind == b'IDAT' and damage == 'pixels':
            original[offset + 8:offset + 8 + length] = b'\0' * length
            original[end - 4:end] = struct.pack('>I', zlib.crc32(original[offset + 4:end - 4]))
        if kind == b'zTXt' and damage == 'text_limit':
            data = b'EINVOICE\0' + b'0' + zlib.compress(b'x' * (PngImagePlugin.MAX_TEXT_CHUNK + 1))
            chunk = struct.pack('>I', len(data)) + b'zTXt' + data
            original[offset:end] = chunk + struct.pack('>I', zlib.crc32(chunk[4:]))
            break
        offset = end
    if damage == 'truncated':
        original = original[:-12]
    source = tmp_path / 'bad.png'
    source.write_bytes(original)

    report = recognition.recognize(files=[source], output_dir=tmp_path / 'report')

    assert report['status'] == 'failed'
    assert report['error_code'] == 'attachment_image_invalid'
    assert requests == []
    assert source.read_bytes() == original
