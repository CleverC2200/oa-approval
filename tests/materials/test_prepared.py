from __future__ import annotations

from apps.oa.materials.formats import prepare_materials
from apps.oa.materials.prepared import read_prepared

import pytest


def test_prepared_projection_preserves_identity_without_sharing_mutable_records(tmp_path):
    import fitz

    source = tmp_path / 'receipt.pdf'
    with fitz.open() as document:
        document.new_page().insert_text((20, 20), 'Original total 25.00')
        document.save(source)
    result = prepare_materials([
        {'resource_id': 'receipt', 'name': 'receipt.pdf', 'path': str(source)},
    ], out_dir=tmp_path / 'prepared')
    first = result.to_record()
    first['images'][0]['resource_id'] = 'changed'
    first['structured_contents'][0]['key_text'].append('changed')
    second = result.to_record()
    assert second['images'][0]['resource_id'] == 'receipt'
    assert second['structured_contents'][0]['key_text'] == ['P1: Original total 25.00']
    coverage = result.coverage('receipt')
    assert coverage.manifest_count == 1
    assert coverage.direct_image_count == coverage.related_image_count == 1
    assert coverage.declared_page_count == 1
    assert coverage.direct_source_sha256 is not None


def test_legacy_prepared_roundtrip_retains_unknown_fields_and_flat_order():
    record = {
        'extension': {'version': 7},
        'images': [{'resource_id': 'second', 'extension': ['keep']}, {'resource_id': 'first'}],
        'manifest': [],
        'structured_contents': [{'resource_id': 'second', 'text': 'Original 001.00'}],
    }
    result = read_prepared(record)
    record['extension']['version'] = 8
    assert result.to_record() == {
        'extension': {'version': 7},
        'images': [{'resource_id': 'second', 'extension': ['keep']}, {'resource_id': 'first'}],
        'manifest': [],
        'structured_contents': [{'resource_id': 'second', 'text': 'Original 001.00'}],
    }
    text_only = {'structured_contents': [{'resource_id': 'text', 'text': 'Original 001.00'}]}
    assert read_prepared(text_only, reading='local').to_record() == text_only


@pytest.mark.parametrize('second_digest', [None, 'bad', 'b' * 64])
def test_direct_coverage_never_filters_missing_invalid_or_conflicting_source_hashes(second_digest):
    result = read_prepared({
        'manifest': [{'resource_id': 'receipt', 'status': 'pdf_rendered', 'page_count': 2, 'image_count': 2}],
        'images': [
            {'resource_id': 'receipt', 'source_content_sha256': 'a' * 64},
            {'resource_id': 'receipt', 'source_content_sha256': second_digest},
            {'resource_id': 'child', 'parent_resource_id': 'receipt', 'source_content_sha256': 'a' * 64},
        ],
    })
    coverage = result.coverage('receipt')
    assert coverage.direct_image_count == 2
    assert coverage.related_image_count == 3
    assert coverage.direct_source_sha256 is None


def test_archive_docx_keeps_local_child_identity_and_existing_outer_parent(tmp_path):
    import zipfile
    from io import BytesIO
    from PIL import Image

    image = BytesIO()
    Image.new('RGB', (2, 2), 'white').save(image, format='PNG')
    source = tmp_path / 'contract.docx'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('word/document.xml', '<document><body/></document>')
        archive.writestr('word/media/image1.png', image.getvalue())
    prepared = prepare_materials([{
        'resource_id': 'archive', 'name': 'archive.zip', 'extracted_files': [{
            'resource_id': 'archive::2', 'parent_resource_id': 'archive',
            'name': 'contract.docx', 'path': str(source),
        }],
    }], out_dir=tmp_path / 'prepared').to_record()
    page = prepared['images'][0]
    assert (page['resource_id'], page['parent_resource_id'], page['name']) == (
        'archive::2::docx_media::1', 'archive', 'contract.docx::word/media/image1.png',
    )
    assert prepared['structured_contents'][0]['resource_id'] == 'archive::2'
    assert prepared['structured_contents'][0]['parent_resource_id'] == 'archive'
    assert [item['resource_id'] for item in prepared['manifest']] == ['archive']
    assert 'parent_resource_id' not in prepared['manifest'][0]
