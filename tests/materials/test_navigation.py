from __future__ import annotations

import json

from apps.oa.materials import cli
from apps.oa.materials.read import read


def test_outline_locates_text_only_material_without_expanding_body(tmp_path, capsys):
    material = {
        'schema_version': 1, 'request_id': 'synthetic-1', 'workflow_key': 'general_expense',
        'declared_fields': [{'path': 'main[0].field[0]', 'value': 'Synthetic field body'}],
        'resources': [{'resource_id': 'xml-1', 'owner_detail_id': None,
                       'source_field_path': 'main[0].field[0]', 'local_readonly_path': '/synthetic/invoice.xml'}],
        'prepared': {'images': [], 'manifest': [], 'structured_contents': [
            {'resource_id': 'xml-1', 'document_type': 'xml', 'text': 'Synthetic attachment body'}]},
    }
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    original = material_file.read_bytes()
    full = read(material_file)

    assert cli.main(['read', str(material_file), '--view', 'outline', '--format', 'json']) == 0
    result = json.loads(capsys.readouterr().out)
    outline = result['outline']
    assert outline['coverage'] == 'navigation_only'
    assert outline['counts'] == {
        'declared_fields': 1, 'detail_tables': 0, 'root_resources': 1, 'logical_resources': 1,
        'archive_child_resources': 0, 'prepared_images': 0, 'structured_contents': 1,
    }
    assert outline['navigation']['fields'] == [{
        'pointer': '/declared_fields/0', 'text_section': 'material.declared_fields',
        'text_index': 0, 'field_path': 'main[0].field[0]'}]
    assert outline['navigation']['structured_contents'][0] == {
        'pointer': '/prepared/structured_contents/0', 'text_section': 'material.prepared.structured_contents',
        'text_index': 0, 'resource_id': 'xml-1', 'parent_resource_id': None,
    }
    assert outline['navigation']['images'] == []
    resource = outline['navigation']['resources'][0]
    assert resource['pointer'] == '/resources/0'
    assert resource['source_field_path'] == 'main[0].field[0]'
    assert resource['original_path'] == '/synthetic/invoice.xml'
    assert 'Synthetic field body' not in json.dumps(outline)
    assert 'Synthetic attachment body' not in json.dumps(outline)
    assert material_file.read_bytes() == original
    assert read(material_file) == full
    assert cli.main(['read', str(material_file), '--view', 'outline']) == 0
    assert json.loads(capsys.readouterr().out) == outline


def test_outline_counts_large_details_and_keeps_duplicate_archive_ownership(tmp_path, capsys):
    fields = [{'path': 'main[0].field[0]', 'value': 'Main body'}]
    fields.extend({'path': f'detail[0].row[{row}].field[{column}]', 'value': f'Body {row} {column}'}
                  for row in range(1000) for column in range(2))
    fields.extend([{'path': 'detail[1].row[0].field[0]', 'value': 'Second table first'},
                   {'path': 'detail[1].row[0].field[1]', 'value': 'Second table other'},
                   {'path': 'detail[1].row[1].field[0]', 'value': 'Second table last'}])
    material = {'declared_fields': fields, 'resources': [
        {'resource_id': rid, 'owner_detail_id': owner, 'source_field_path': owner + '.field[1]',
         'path': '/synthetic/' + rid + '.zip', 'content_sha256': digest,
         'extracted_files': [{'resource_id': rid + '::1', 'parent_resource_id': rid,
                              'archive_path': 'nested.zip::invoice.pdf',
                              'path': '/synthetic/' + rid + '/invoice.pdf', 'content_sha256': 'c' * 64}]}
        for rid, owner, digest in [('a', 'detail[0].row[999]', 'a' * 64),
                                   ('b', 'detail[1].row[1]', 'b' * 64)]],
        'prepared': {'images': [
            {'resource_id': rid + '::1', 'parent_resource_id': rid,
             'path': f'/synthetic/shared/page-{page}.png', 'source_content_sha256': 'c' * 64}
            for rid in ('a', 'b') for page in (1, 2)],
            'structured_contents': [
                {'resource_id': rid + '::1', 'parent_resource_id': rid,
                 'source_content_sha256': 'c' * 64, 'key_text': ['Unexpanded original text']}
                for rid in ('a', 'b')], 'manifest': []}}
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    original = material_file.read_bytes()

    assert cli.main(['read', str(material_file), '--view', 'outline', '--format', 'json']) == 0
    outline = json.loads(capsys.readouterr().out)['outline']
    assert outline['counts'] == {
        'declared_fields': 2004, 'detail_tables': 2, 'root_resources': 2, 'logical_resources': 4,
        'archive_child_resources': 2, 'prepared_images': 4, 'structured_contents': 2,
    }
    assert outline['detail_tables'] == [
        {'field_path': 'detail[0]', 'row_count': 1000, 'field_count': 2000},
        {'field_path': 'detail[1]', 'row_count': 2, 'field_count': 3},
    ]
    navigation = outline['navigation']
    assert navigation['fields'][2003] == {
        'pointer': '/declared_fields/2003', 'text_section': 'material.declared_fields',
        'text_index': 2003, 'field_path': 'detail[1].row[1].field[0]'}
    resources = navigation['resources']
    assert [(r['resource_id'], r['parent_resource_id'], r['pointer']) for r in resources] == [
        ('a', None, '/resources/0'), ('a::1', 'a', '/resources/0/extracted_files/0'),
        ('b', None, '/resources/1'), ('b::1', 'b', '/resources/1/extracted_files/0')]
    assert resources[1]['archive_path'] == 'nested.zip::invoice.pdf'
    assert resources[0]['owner_detail_id'] == 'detail[0].row[999]'
    assert resources[2]['owner_detail_id'] == 'detail[1].row[1]'
    assert [(p['resource_id'], p['parent_resource_id'], p['page_number'], p['evidence_ref'])
            for p in navigation['images']] == [
        ('a::1', 'a', 1, 'material:prepared.images.a.1'),
        ('a::1', 'a', 2, 'material:prepared.images.a.2'),
        ('b::1', 'b', 1, 'material:prepared.images.b.1'),
        ('b::1', 'b', 2, 'material:prepared.images.b.2')]
    assert navigation['images'][3]['pointer'] == '/prepared/images/3'
    assert outline['content_reuse']['matching_source_content'] == [{
        'content_sha256': 'c' * 64, 'sources': [
            {'resource_id': 'a::1', 'parent_resource_id': 'a'},
            {'resource_id': 'b::1', 'parent_resource_id': 'b'}]}]
    assert outline['content_reuse']['shared_prepared_paths'] == [
        {'path': '/synthetic/shared/page-1.png', 'pointers': ['/prepared/images/0', '/prepared/images/2']},
        {'path': '/synthetic/shared/page-2.png', 'pointers': ['/prepared/images/1', '/prepared/images/3']}]
    assert 'Unexpanded original text' not in json.dumps(outline)
    full = read(material_file)
    assert '[material.declared_fields]' in full
    assert '明细表2第2行：detail[1].row[1]' in full
    assert '2003: ["detail[1].row[1].field[0]","Second table last"]' in full
    assert material_file.read_bytes() == original


def test_outline_rejects_invalid_collection_shapes_without_traceback(tmp_path, capsys):
    import pytest

    material_file = tmp_path / 'material.json'
    for material in ({'declared_fields': {}}, {'resources': [None]},
                     {'prepared': []}, {'prepared': {'images': ['not a page']}},
                     {'resources': [{'extracted_files': [1]}]}):
        material_file.write_text(json.dumps(material))
        with pytest.raises(ValueError, match='material_file_invalid'):
            read(material_file, view='outline')
        assert cli.main(['read', str(material_file), '--view', 'outline']) == 1
        assert json.loads(capsys.readouterr().out) == {'status': 'failed', 'error_code': 'material_file_invalid'}


def test_outline_of_frozen_collection_keeps_rules_and_submission_integrity(tmp_path, capsys):
    from tests.approvals.test_operations import service

    app, api = service(tmp_path)
    run = app.pending()
    task = app.collect(run['run_id'], agent_id='travel-agent', expected_task_id=1,
                       agent_workflow_key='travel_expense')
    from pathlib import Path

    material_file = Path(task['material_file'])
    rule_file = Path(task['rule_file'])
    before = material_file.read_bytes(), rule_file.read_bytes()
    assert cli.main(['read', str(material_file), '--view', 'outline', '--format', 'json']) == 0
    outline = json.loads(capsys.readouterr().out)['outline']
    assert outline['identity']['request_id'] == '101'
    assert outline['detail_tables'] == [{'field_path': 'detail[0]', 'row_count': 1, 'field_count': 1}]
    assert outline['counts']['logical_resources'] == 1
    assert outline['counts']['structured_contents'] == 1
    assert (material_file.read_bytes(), rule_file.read_bytes()) == before
    assert app.submit(task['task_id'], opinion='AI审核结论：需人工判断 ⚠️')['status'] == 'preview'
    assert api.posts == []


def test_outline_cli_does_not_load_oa_model_or_material_processors(tmp_path):
    from pathlib import Path
    import subprocess
    import sys

    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps({'declared_fields': [], 'resources': [], 'prepared': {}}))
    code = '''import importlib.abc
import runpy
import sys
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('apps.oa.approvals.sources', 'apps.oa.materials.recognize', 'apps.oa.materials.formats')):
            raise AssertionError('outline_import_side_effect:' + fullname)
sys.meta_path.insert(0, Refuse())
sys.path.insert(0, sys.argv.pop(1))
runpy.run_module('apps.oa.materials', run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(Path(__file__).resolve().parents[2]),
                             'read', str(material_file), '--view', 'outline', '--format', 'json'],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['outline']['coverage'] == 'navigation_only'
    assert list(tmp_path.iterdir()) == [material_file]


def test_outline_reports_matching_frozen_source_hashes_without_prepared_content(tmp_path, capsys):
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps({'resources': [
        {'resource_id': 'first', 'source_content_sha256': 'a' * 64, 'owner_detail_id': 'detail[0].row[0]'},
        {'resource_id': 'second', 'source_content_sha256': 'a' * 64, 'owner_detail_id': 'detail[0].row[1]'}]}))
    assert cli.main(['read', str(material_file), '--view', 'outline', '--format', 'json']) == 0
    outline = json.loads(capsys.readouterr().out)['outline']
    assert outline['counts']['logical_resources'] == 2
    assert outline['content_reuse']['matching_source_content'] == [{
        'content_sha256': 'a' * 64, 'sources': [
            {'resource_id': 'first', 'parent_resource_id': None},
            {'resource_id': 'second', 'parent_resource_id': None}]}]
