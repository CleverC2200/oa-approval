import hashlib
import json

import pytest

from apps.oa.materials import cli
from apps.oa.materials.read import read


def test_cli_pages_reconstruct_the_complete_material_without_source_changes(tmp_path, capsys):
    path = tmp_path / 'material.json'
    path.write_text(json.dumps({'declared_fields': [{'path': 'main[0].field[0]', 'value': '001.00'}],
        'authority_facts': [{'value': '首部\n' + '材料甲乙😀\\"' * 1500 + '\n尾部'}]}, ensure_ascii=False))
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    offset, chunks = 0, []
    while offset is not None:
        assert cli.main(['read', str(path), '--format', 'json', '--page-offset', str(offset),
                         '--expected-sha256', digest]) == 0
        page = json.loads(capsys.readouterr().out)
        assert page['status'] == 'ok' and page['coverage'] == 'range_only'
        assert page['material_sha256'] == digest and page['offset'] == offset
        assert len(page['text']) <= 2000
        assert page['end'] == offset + len(page['text'])
        chunks.append(page['text'])
        offset = page['next_offset']
    assert ''.join(chunks) == read(path)
    assert path.read_bytes() == raw


def test_selected_pointer_pages_preserve_exact_string_and_array_identity(tmp_path):
    from apps.oa.materials.read import read_page

    path = tmp_path / 'material.json'
    text = '{"order_detail":{"amount":"001.00","text":"甲\\n乙"}}'
    path.write_text(json.dumps({'authority_facts': [{'~value/x': text}]}))
    page = read_page(path, pointer='/authority_facts/0/~0value~1x', offset=0)
    assert page['text'] == text
    assert page['source_pointer'] == '/authority_facts/0/~0value~1x'
    assert page['next_offset'] is None and page['total_chars'] == len(text)


def test_next_page_requires_the_same_material_version(tmp_path):
    from apps.oa.materials.read import read_page

    path = tmp_path / 'material.json'
    path.write_text('{"value":"abcdef"}')
    first = read_page(path, offset=0, page_chars=2)
    with pytest.raises(ValueError, match='material_read_version_required'):
        read_page(path, offset=2)
    path.write_text('{"value":"changed"}')
    with pytest.raises(ValueError, match='material_read_version_changed'):
        read_page(path, offset=2, expected_sha256=first['material_sha256'])


def test_large_outline_is_also_bounded_and_is_not_reading_coverage(tmp_path, capsys):
    path = tmp_path / 'material.json'
    path.write_text(json.dumps({'declared_fields': [{'path': f'detail[0].row[{i}].field[0]', 'value': str(i)} for i in range(600)]}))
    assert cli.main(['read', str(path), '--view', 'outline', '--format', 'json', '--page-offset', '0']) == 0
    page = json.loads(capsys.readouterr().out)
    assert page['coverage'] == 'navigation_range_only' and page['next_offset'] == 2000
    assert len(page['text']) == 2000


@pytest.mark.parametrize('options', [
    {'offset': -1}, {'offset': 999}, {'offset': 0, 'page_chars': 0},
    {'offset': 0, 'page_chars': 2001}, {'offset': 0, 'pointer': '/missing'},
    {'offset': 0, 'pointer': '/array/-1'}, {'offset': 0, 'pointer': '/array/01'},
])
def test_invalid_page_or_pointer_is_rejected_without_writes(tmp_path, options):
    from apps.oa.materials.read import read_page

    path = tmp_path / 'material.json'
    path.write_text('{"array":[1,2]}')
    raw = path.read_bytes()
    with pytest.raises(ValueError):
        read_page(path, **options)
    assert path.read_bytes() == raw
