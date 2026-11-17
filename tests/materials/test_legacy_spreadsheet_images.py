from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import fitz
import pytest

from apps.oa.materials.prepare import prepare


def biff(kind, content=b''):
    return struct.pack('<HH', kind, len(content)) + content


def art(kind, content=b'', *, version=0, instance=0):
    return struct.pack('<HHI', version | instance << 4, kind, len(content)) + content


def compound(workbook):
    stream = workbook.ljust(max(4096, (len(workbook) + 511) // 512 * 512), b'\0')
    sectors = len(stream) // 512
    header = bytearray(512)
    header[:8] = bytes.fromhex('d0cf11e0a1b11ae1')
    struct.pack_into('<HHHH', header, 24, 0x3e, 3, 0xfffe, 9)
    struct.pack_into('<H', header, 32, 6)
    struct.pack_into('<IIIIIIIII', header, 40, 0, 1, sectors, 0, 4096, 0xfffffffe, 0, 0xfffffffe, 0)
    struct.pack_into('<109I', header, 76, sectors + 1, *([0xffffffff] * 108))

    def entry(name, kind, first, size, child=0xffffffff):
        item = bytearray(128)
        encoded = (name + '\0').encode('utf-16le')
        item[:len(encoded)] = encoded
        struct.pack_into('<HBBIII', item, 64, len(encoded), kind, 1, 0xffffffff, 0xffffffff, child)
        struct.pack_into('<IQ', item, 116, first, size)
        return bytes(item)

    directory = entry('Root Entry', 5, 0xfffffffe, 0, child=1) + entry('Workbook', 2, 0, len(stream))
    allocation = list(range(1, sectors)) + [0xfffffffe, 0xfffffffe, 0xfffffffd]
    fat = struct.pack('<128I', *allocation, *([0xffffffff] * (128 - len(allocation))))
    return bytes(header) + stream + directory.ljust(512, b'\0') + fat


def legacy_book(path, *, defect=None, continued=False, named=False):
    pixels = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    pixels.clear_with(255)
    png = pixels.tobytes('png')
    image_bytes = b'invalid image' if defect == 'corrupt_image' else png
    blip = art(0xf01a if defect == 'unsupported_blip' else 0xf01e, b'P' * 16 + b'\xff' + image_bytes, instance=0x6e0)
    entry = bytes([6, 6]) + b'P' * 16 + struct.pack('<HIII', 0xff, len(blip), 2, 0) + bytes(4) + blip
    entries = art(0xf007, entry, version=2, instance=6)
    if defect == 'unreferenced_blip':
        entries += art(0xf007, entry, version=2, instance=6)
    store = art(0xf001, entries, version=15, instance=2 if defect == 'unreferenced_blip' else 1)
    drawings = art(0xf000, store, version=15)
    sheets = []
    for index in range(2):
        shape = art(0xf00a, struct.pack('<II', 1025 + index, 0xa00), version=2, instance=75)
        properties = struct.pack('<HI', 0x4104, 2 if defect == 'missing_reference' else 1)
        if named:
            if named == 'simple':
                properties += struct.pack('<HI', 0x4105, 0)
            else:
                image_name = 'synthetic.png\0'.encode('utf-16le')
                properties += struct.pack('<HI', 0xc105, len(image_name)) + image_name
        shape += art(0xf00b, properties, version=3, instance=2 if named else 1)
        anchor = struct.pack('<9H', 0, index + 1, 0, index + 2, 0, index + 2, 0, index + 3, 0)
        if defect != 'missing_anchor':
            shape += art(0xf010, anchor)
        shape += art(0xf011)
        if defect == 'unknown_shape_content':
            shape += art(0xf00d, b'unread text')
        drawing = art(0xf002, art(0xf003, art(0xf004, shape, version=15), version=15), version=15)
        if defect == 'truncated_drawing':
            drawing = drawing[:-1]
        obj = struct.pack('<HHHHH', 0x15, 18, 8, index + 1, 0x6011) + bytes(12)
        obj += biff(7, struct.pack('<H', 0xffff))
        obj += biff(8, struct.pack('<H', 2 if defect == 'linked_picture' else 1)) + bytes(4)
        if defect == 'object_without_drawing' and index == 1:
            drawing = b''
        bof = biff(0x809, struct.pack('<HHHHII', 0x600, 0x10, 0xdbb, 0x7cc, 0, 6))
        label = biff(0x204, struct.pack('<HHHHB', 0, 0, 0, 5, 0) + b'value')
        extra = biff({'header_picture': 0x866, 'background_picture': 0xe9}[defect], b'picture') if defect in {'header_picture', 'background_picture'} else b''
        drawing_records = biff(0xec, drawing) if drawing else b''
        if continued:
            drawing_records = biff(0xec, drawing[:23]) + biff(0x3c, drawing[23:])
        sheets.append(bof + label + drawing_records + biff(0x5d, obj) + extra + biff(0xa))
    bof = biff(0x809, struct.pack('<HHHHII', 0x600, 5, 0xdbb, 0x7cc, 0, 6))
    names = ['First', 'Second']
    bounds = [struct.pack('<IBBBB', 0, 0, 0, len(name), 0) + name.encode() for name in names]
    group_records = biff(0xeb, drawings)
    if continued:
        group_records = biff(0xeb, drawings[:101]) + biff(0x3c, drawings[101:])
    globals_size = len(bof) + sum(4 + len(bound) for bound in bounds) + len(group_records) + 4
    position = globals_size
    for index, sheet in enumerate(sheets):
        bounds[index] = struct.pack('<I', position) + bounds[index][4:]
        position += len(sheet)
    if defect == 'duplicate_sheet_offset':
        bounds[1] = bounds[0][:4] + bounds[1][4:]
    workbook = bof + b''.join(biff(0x85, bound) for bound in bounds) + group_records + biff(0xa) + b''.join(sheets)
    path.write_bytes(compound(workbook))
    return png


@pytest.mark.parametrize('continued', [False, True])
def test_prepare_legacy_multisheet_images_preserves_each_resource_and_parent(tmp_path, monkeypatch, continued):
    source = tmp_path / 'photos.xls'
    png = legacy_book(source, continued=continued)
    original = source.read_bytes()
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'resources': [
        {'resource_id': 'first-book', 'path': str(source), 'parent_resource_id': 'first-archive'},
        {'resource_id': 'second-book', 'path': str(source), 'parent_resource_id': 'second-archive'},
    ]}))
    result = prepare(manifest=manifest, output_dir=tmp_path / 'prepared')
    assert result['status'] == 'complete'
    material = json.loads(Path(result['material_file']).read_text())
    images = material['prepared']['images']
    assert len(images) == 4
    for resource, parent in [('first-book', 'first-archive'), ('second-book', 'second-archive')]:
        owned = [image for image in images if image['resource_id'] == resource]
        assert {image['spreadsheet_cell'] for image in owned} == {'First!B3', 'Second!C4'}
        assert all(image['parent_resource_id'] == parent for image in owned)
        assert all(image['source_resource_id'] == resource for image in owned)
        assert all(image['source_content_sha256'] == hashlib.sha256(original).hexdigest() for image in owned)
        assert all(Path(image['path']).read_bytes() == png for image in owned)
    assert source.read_bytes() == original
    assert len(material['prepared']['structured_contents']) == 2
    assert all('value' in str(fact['key_text']) for fact in material['prepared']['structured_contents'])

    from apps.oa.materials import recognize as recognition

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'synthetic-key')
    calls = []

    def post(body, **_options):
        calls.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'synthetic photo'}}]}

    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(material_file=Path(result['material_file']), output_dir=tmp_path / 'recognition')
    assert report['status'] == 'complete'
    assert report['coverage'] == 'all_prepared_content'
    assert len(calls) == 1
    pages = [part for part in report['parts'] if part['kind'] == 'image']
    assert {(part['prepared_resource_id'], part['resource_id'], part['spreadsheet_cell']) for part in pages} == {
        ('first-book', 'first-archive', 'First!B3'), ('first-book', 'first-archive', 'Second!C4'),
        ('second-book', 'second-archive', 'First!B3'), ('second-book', 'second-archive', 'Second!C4'),
    }


def test_prepare_rejects_images_without_sheet_ownership(tmp_path):
    source = tmp_path / 'unowned.xls'
    legacy_book(source, defect='unreferenced_blip')
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'failed'
    assert result['error_code'] == 'attachment_content_incomplete'
    material = json.loads(Path(result['material_file']).read_text())
    assert material['prepared']['images'] == []


@pytest.mark.parametrize('defect', ['header_picture', 'background_picture'])
def test_prepare_rejects_unread_picture_record_families(tmp_path, defect):
    source = tmp_path / 'unsupported.xls'
    legacy_book(source, defect=defect)
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'failed'
    assert result['error_code'] == 'attachment_content_incomplete'


def test_prepare_rejects_linked_picture_objects(tmp_path):
    source = tmp_path / 'linked.xls'
    legacy_book(source, defect='linked_picture')
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'failed'
    assert result['error_code'] == 'attachment_content_incomplete'


def test_prepare_rejects_ambiguous_sheet_stream_ownership(tmp_path):
    source = tmp_path / 'ambiguous.xls'
    legacy_book(source, defect='duplicate_sheet_offset')
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'failed'
    assert result['error_code'] == 'attachment_content_incomplete'


@pytest.mark.parametrize('suffix', ['.xlsx', '.xls'])
def test_prepare_ooxml_images_keeps_sheet_and_anchor_despite_suffix(tmp_path, suffix):
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.drawing.image import Image

    pixels = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    pixels.clear_with(255)
    workbook = Workbook()
    workbook.active.title = 'First'
    workbook.active.add_image(Image(BytesIO(pixels.tobytes('png'))), 'D5')
    workbook.create_sheet('Second').add_image(Image(BytesIO(pixels.tobytes('png'))), 'E6')
    source = tmp_path / ('ooxml' + suffix)
    workbook.save(source)
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'complete'
    material = json.loads(Path(result['material_file']).read_text())
    assert {image['spreadsheet_cell'] for image in material['prepared']['images']} == {'First!D5', 'Second!E6'}


def test_prepare_text_only_legacy_workbook_still_completes(tmp_path):
    source = Path(__file__).parents[1] / 'fixtures' / 'legacy-workbook.xls'
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'complete'
    assert result['images'] == 0
    assert result['structured_contents'] == 1


def test_prepare_rejects_unread_shape_content(tmp_path):
    source = tmp_path / 'shape.xls'
    legacy_book(source, defect='unknown_shape_content')
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'failed'
    assert result['error_code'] == 'attachment_content_incomplete'


@pytest.mark.parametrize('defect', [
    'object_without_drawing', 'missing_anchor', 'missing_reference',
    'corrupt_image', 'unsupported_blip', 'truncated_drawing',
])
def test_prepare_rejects_incomplete_or_unsupported_legacy_images(tmp_path, defect):
    source = tmp_path / 'broken.xls'
    legacy_book(source, defect=defect)
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'failed'
    assert result['error_code'] == 'attachment_content_incomplete'


@pytest.mark.parametrize('named', [True, 'simple'])
def test_prepare_keeps_images_with_picture_name_metadata(tmp_path, named):
    source = tmp_path / 'named.xls'
    legacy_book(source, named=named)
    result = prepare(files=[source], output_dir=tmp_path / 'prepared')
    assert result['status'] == 'complete'
    assert result['images'] == 2
