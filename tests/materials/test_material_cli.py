from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


PROJECT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('module,arguments', [
    ('apps.oa', ['--help']),
    ('apps.oa.materials', ['--help']),
    ('apps.oa.materials', ['prepare', '--help']),
    ('apps.oa.materials', ['recognize', '--help']),
    ('apps.oa.materials', ['compress', '--help']),
    ('apps.oa.references', ['import-tpm', '--help']),
])
def test_capability_help_does_not_load_processors_or_credentials(tmp_path, module, arguments):
    code = '''import importlib.abc
import runpy
import sys
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'fitz', 'PIL', 'openpyxl', 'apps.oa.approvals.sources.auth'}:
            raise AssertionError('help_import_side_effect:' + fullname)
sys.meta_path.insert(0, Refuse())
sys.path.insert(0, sys.argv.pop(1))
module = sys.argv.pop(1)
runpy.run_module(module, run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(PROJECT), module, *arguments],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_material_read_cli_preserves_text_and_can_return_json(tmp_path, capsys):
    from apps.oa.materials import cli
    from apps.oa.materials.read import read

    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps({'request_id': '101', 'declared_fields': [{'value': '12.50'}]}))
    assert cli.main(['read', str(material_file)]) == 0
    assert capsys.readouterr().out == read(material_file)
    assert cli.main(['read', str(material_file), '--format', 'json']) == 0
    assert json.loads(capsys.readouterr().out) == {
        'status': 'complete', 'material_file': str(material_file), 'text': read(material_file)}


def test_prepare_cli_keeps_two_archive_parents_and_complete_local_material(tmp_path, capsys):
    from io import BytesIO
    from zipfile import ZipFile

    import fitz
    from openpyxl import Workbook

    from apps.oa.materials import cli

    document = fitz.open()
    document.new_page().insert_text((72, 72), 'first page')
    document.new_page().insert_text((72, 72), 'second page')
    pdf = document.tobytes()
    document.close()
    workbook = Workbook()
    workbook.active.append(['quantity', 'amount'])
    workbook.active.append([2, 12.50])
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    nested = BytesIO()
    with ZipFile(nested, 'w') as archive:
        archive.writestr('same.pdf', pdf)
    originals = []
    for name in ('first.zip', 'second.zip'):
        path = tmp_path / name
        with ZipFile(path, 'w') as archive:
            archive.writestr('nested.zip', nested.getvalue())
            archive.writestr('same.xlsx', stream.getvalue())
        originals.append(path)
    manifest_file = tmp_path / 'originals.json'
    manifest_file.write_text(json.dumps({'resources': [
        {'resource_id': rid, 'path': str(path), 'name': path.name, 'source': 'trusted_oa'}
        for rid, path in zip(('parent-a', 'parent-b'), originals)]}))
    before = [path.read_bytes() for path in originals]
    output = tmp_path / 'prepared'

    assert cli.main(['prepare', '--manifest', str(manifest_file), '--output-dir', str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    material = json.loads(Path(result['material_file']).read_text())
    assert material['kind'] == 'local_material'
    assert material.get('request_id') is None
    assert [resource['resource_id'] for resource in material['resources']] == ['parent-a', 'parent-b']
    assert all(resource['source'] == 'local' for resource in material['resources'])
    assert len(material['prepared']['images']) == 4
    assert {page['parent_resource_id'] for page in material['prepared']['images']} == {'parent-a', 'parent-b'}
    assert len({page['path'] for page in material['prepared']['images']}) == 4
    spreadsheets = [item for item in material['prepared']['structured_contents'] if item['document_type'] == 'spreadsheet']
    assert len(spreadsheets) == 2
    assert {item['parent_resource_id'] for item in spreadsheets} == {'parent-a', 'parent-b'}
    assert all(len(resource['content_sha256']) == 64 for resource in material['resources'])
    assert [path.read_bytes() for path in originals] == before
    artifact_before = Path(result['material_file']).read_bytes()
    assert cli.main(['prepare', '--manifest', str(manifest_file), '--output-dir', str(output)]) == 1
    assert json.loads(capsys.readouterr().out)['error_code'] == 'material_output_exists'
    assert Path(result['material_file']).read_bytes() == artifact_before


def test_prepare_reuses_existing_expansion_without_removing_old_children(tmp_path):
    from zipfile import ZipFile

    from apps.oa.materials.prepare import prepare

    archive_path = tmp_path / 'original.zip'
    with ZipFile(archive_path, 'w') as archive:
        archive.writestr('memo.xml', '<memo>keep original facts</memo>')
    old = tmp_path / 'old-expansion'
    old.mkdir()
    child = old / 'memo.xml'
    child.write_text('<memo>keep original facts</memo>')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'resources': [{
        'resource_id': 'existing-parent', 'path': str(archive_path), 'archive_status': 'extracted',
        'extracted_files': [{'resource_id': 'existing-parent::1', 'parent_resource_id': 'existing-parent',
                             'name': 'memo.xml', 'path': str(child), 'archive_path': 'memo.xml'}]}]}))
    original = child.read_bytes()
    result = prepare(manifest=manifest, output_dir=tmp_path / 'new-output')
    assert result['status'] == 'complete'
    material = json.loads(Path(result['material_file']).read_text())
    assert material['resources'][0]['extracted_files'][0]['resource_id'] == 'existing-parent::1'
    assert material['resources'][0]['extracted_files'][0]['path'] == str(child)
    assert child.read_bytes() == original
    assert list(old.iterdir()) == [child]


def test_material_cli_resolves_relative_input_from_another_working_directory(tmp_path):
    source = tmp_path / 'invoice.xml'
    source.write_text('<Invoice><InvoiceNumber>LOCAL-1</InvoiceNumber><Amount>12.50</Amount></Invoice>')
    command = ['bash', str(PROJECT / '.codex/python.sh'), '-m', 'apps.oa.materials']
    prepared = subprocess.run([*command, 'prepare', '--file', 'invoice.xml', '--output-dir', 'output'],
                              cwd=tmp_path, capture_output=True, text=True)
    assert prepared.returncode == 0, prepared.stderr + prepared.stdout
    material_file = Path(json.loads(prepared.stdout)['material_file'])
    assert material_file == tmp_path / 'output/material.json'
    read = subprocess.run([*command, 'read', 'output/material.json'], cwd=tmp_path, capture_output=True, text=True)
    assert read.returncode == 0, read.stderr
    assert 'LOCAL-1' in read.stdout
    assert not (tmp_path / 'runtime').exists()
