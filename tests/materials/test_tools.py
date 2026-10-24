from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile

from PIL import Image
import pytest


def test_deepseek_reads_prepared_material_without_legacy_action(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-api-key')
    from apps.oa.materials import recognize as attachment_recognition

    image = tmp_path / 'invoice.png'
    Image.new('RGB', (12, 8), 'white').save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps({'request_id': '12', 'prepared': {
        'images': [{'resource_id': 'r1', 'path': str(image), 'content_sha256': digest}],
        'manifest': [{'resource_id': 'r1', 'status': 'image', 'image_count': 1}],
        'structured_contents': [],
    }}))
    requests = []

    def post(body, *, key, timeout):
        requests.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '金额 25 元'}}]}

    monkeypatch.setattr(attachment_recognition, '_post', post)
    result = attachment_recognition.recognize(material_file=material_file,
                                               output_dir=tmp_path / 'recognized')
    assert result['status'] == 'complete'
    assert len(requests) == 1
    assert result['parts'][0]['resource_id'] == 'r1'
    assert result['parts'][0]['page_number'] == 1
    assert result['parts'][0]['source'] == 'deepseek'
    assert result['parts'][0]['answer'] == '金额 25 元'
    assert json.loads(Path(result['report_file']).read_text())['material_binding']['request_id'] == '12'


def test_parser_returns_complete_structured_content_without_api_key(tmp_path, monkeypatch):
    from apps.oa.materials import recognize as attachment_recognition

    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    monkeypatch.setenv('OA_REVIEW_API_KEY', '')
    content = {'resource_id': 'archive:costs.xlsx', 'parent_resource_id': 'archive',
               'sheet_name': 'Costs', 'key_text': ['A1: 原始金额 001.00', 'B2: 尾页文字' * 6000],
               'cells': [{'cell': 'A1', 'value': '001.00'}]}
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps({'request_id': '12', 'prepared': {
        'images': [], 'manifest': [], 'structured_contents': [content]}}, ensure_ascii=False))

    def post(*args, **kwargs):
        pytest.fail('parser content must not be sent to DeepSeek')

    monkeypatch.setattr(attachment_recognition, '_post', post)
    report = attachment_recognition.recognize(material_file=material_file,
                                               output_dir=tmp_path / 'recognized')
    assert report['status'] == 'complete'
    assert len(report['parts']) == 1
    part = report['parts'][0]
    assert part['source'] == 'parser'
    assert part['resource_id'] == 'archive'
    assert part['prepared_resource_id'] == 'archive:costs.xlsx'
    assert part['evidence_ref'] == 'material:prepared.structured_contents[0]'
    assert part['sheet_name'] == 'Costs'
    assert json.loads(part['answer']) == content
    assert json.loads(Path(report['report_file']).read_text()) == report


def test_deepseek_archive_and_duplicate_pages_keep_real_attachment_ids_in_sqlite(tmp_path, monkeypatch):
    from apps.oa.approvals.store import Store
    from apps.oa.materials import recognize as attachment_recognition

    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-api-key')
    image = tmp_path / 'page.png'
    Image.new('RGB', (12, 8), 'white').save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    material = {'request_id': '12', 'resources': [
        {'resource_id': rid, 'name': rid + '.zip'} for rid in ('4225947', '4228930')],
        'prepared': {'images': [
            {'resource_id': rid + ':member.pdf', 'parent_resource_id': rid,
             'path': str(image), 'content_sha256': digest} for rid in ('4225947', '4228930')],
            'structured_contents': [{'resource_id': '4225947:member.pdf', 'parent_resource_id': '4225947',
                                     'text': 'PDF 确定性文字 25.00'}], 'manifest': []}}
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps(material))
    requests = []

    def post(body, **kwargs):
        requests.append(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '金额 25 元'}}]}

    monkeypatch.setattr(attachment_recognition, '_post', post)
    report = attachment_recognition.recognize(material_file=material_file, output_dir=tmp_path / 'recognized')
    assert report['status'] == 'complete'
    assert [p['resource_id'] for p in report['parts']] == ['4225947', '4228930', '4225947']
    assert [p['prepared_resource_id'] for p in report['parts']] == [
        '4225947:member.pdf', '4228930:member.pdf', '4225947:member.pdf']
    assert report['parts'][1]['reused_from'] == 'image-1'
    assert [p['source'] for p in report['parts']] == ['deepseek', 'deepseek', 'parser']
    assert json.loads(report['parts'][2]['answer']) == material['prepared']['structured_contents'][0]
    assert len(requests) == 1
    assert requests[0]['messages'][1]['content'][1]['type'] == 'image_url'
    store = Store(tmp_path / 'approval.sqlite3')
    run = store.start_run([{'request_id': '12', 'received_at': '2026-10-22 09:00:00',
                           'operator_id': 'tester', 'workflow_key': 'general_expense'}])
    task = store.claim(run, agent_id='agent-a', expected_task_id=1, agent_workflow_key='general_expense')["task"]
    store.freeze(task['id'], material, material_file, 'digest', {str(image): digest}, material_file)
    store.review(task['id'], '合成离线测试意见', report)
    store.review(task['id'], '合成离线测试意见', report)
    rows = store.connection.execute('''SELECT a.resource_id,c.content,c.metadata_json FROM attachment_contents c
        JOIN attachments a ON a.id=c.attachment_id WHERE c.source='deepseek' ORDER BY c.id''').fetchall()
    assert [(r['resource_id'], r['content']) for r in rows] == [
        ('4225947', '金额 25 元'), ('4228930', '金额 25 元')]
    assert json.loads(rows[1]['metadata_json'])['reused_from'] == 'image-1'
    assert json.loads(rows[1]['metadata_json'])['prepared_resource_id'] == '4228930:member.pdf'
    assert store.details(task['id'])['submissions'] == []


def test_archive_pdf_and_excel_preserve_all_content_and_logical_resources(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    import fitz
    from openpyxl import Workbook
    from apps.oa.approvals.collection import downloads as attachments
    from apps.oa.materials import archives
    from apps.oa.materials import formats as attachment_extractors

    pdf = tmp_path / 'invoice.pdf'
    with fitz.open() as document:
        document.new_page().insert_text((20, 20), 'Page one')
        document.new_page().insert_text((20, 20), 'Page two')
        document.save(pdf)
    excel = tmp_path / 'detail.xlsx'
    workbook = Workbook()
    workbook.active.title = 'Costs'
    workbook.active.append(['Item', 'Amount'])
    workbook.active.append(['Taxi', 25])
    workbook.create_sheet('More').append(['Last sheet'])
    workbook.save(excel)
    archive = tmp_path / 'files.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.write(pdf, 'receipts/invoice.pdf')
    children, error = archives.extract_zip_safely(
        archive, dest_dir=tmp_path / 'downloads', resource={'id': 'archive', 'name': 'files.zip'})
    assert error == ''
    images, manifest, contents = attachment_extractors.prepare_materials([
        {'resource_id': 'archive', 'path': str(archive), 'extracted_files': children},
        {'resource_id': 'detail-a', 'path': str(excel)},
        {'resource_id': 'detail-b', 'path': str(excel)},
    ], out_dir=tmp_path / 'prepared')
    assert len(images) == 2
    assert all(item['parent_resource_id'] == 'archive' for item in images)
    assert manifest[0]['image_count'] == 2
    sheets = {item['resource_id']: item['key_text'] for item in contents if item['document_type'] == 'spreadsheet'}
    assert sheets['detail-a'] == ['Costs!R1: C1=Item | C2=Amount', 'Costs!R2: C1=Taxi | C2=25', 'More!R1: C1=Last sheet']
    assert sheets['detail-b'] == sheets['detail-a']


def test_download_reuses_content_but_retains_each_attachment_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    from apps.oa.approvals.collection import downloads as attachments

    def download(resource, *, dest_dir, timeout_sec, original_batch=None):
        target = dest_dir / (resource['id'] + '.png')
        Image.new('RGB', (10, 10), 'white').save(target)
        return target, ''

    monkeypatch.setattr(attachments, '_download_resource_direct', download)
    resources = [{'id': 'a', 'name': 'A.png', 'downloadUrl': 'https://example.invalid/a'},
                 {'id': 'b', 'name': 'B.png', 'downloadUrl': 'https://example.invalid/b'}]
    downloaded, errors = attachments.download_resources(resources, dest_dir=tmp_path / 'files',
        allow_browser_download=True, timeout_sec=5, request_id='12', flow_key='general_expense', reuse_cache=False)
    assert errors == []
    assert [item['resource_id'] for item in downloaded] == ['a', 'b']
    assert [item['name'] for item in downloaded] == ['A.png', 'B.png']
    assert downloaded[0]['path'] == downloaded[1]['path']
    assert len(list((tmp_path / 'files').glob('*.png'))) == 1


def test_compression_preserves_original_and_creates_bounded_reading_copy(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    from apps.oa.materials.compress import compress_image

    source = tmp_path / 'large.png'
    Image.new('RGB', (600, 400), 'white').save(source)
    original = source.read_bytes()
    result = compress_image(source, output_dir=tmp_path / 'small', max_dimension=120,
                            max_pixels=10_000, max_bytes=100_000)
    assert result['status'] == 'complete'
    assert source.read_bytes() == original
    with Image.open(result['output']['path']) as copy:
        assert copy.size == (120, 80)
    assert result['readability'] == 'not_verified'


def test_deepseek_region_keeps_original_coordinates_and_partial_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-api-key')
    from apps.oa.materials import recognize as attachment_recognition

    source = tmp_path / 'receipt.png'
    Image.new('RGB', (40, 30), 'white').save(source)
    monkeypatch.setattr(attachment_recognition, '_post', lambda *_args, **_kw: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': '25.00'}}]})
    result = attachment_recognition.recognize(files=[source], region=(5, 6, 25, 16),
                                               output_dir=tmp_path / 'region')
    assert result['status'] == 'complete'
    assert result['coverage'] == 'image_region'
    assert result['parts'][0]['source_image_size'] == [40, 30]
    assert result['parts'][0]['image_tiles'][0]['box'] == [5, 6, 25, 16]


def test_office_converter_output_is_read_page_by_page(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    from apps.oa.materials.formats import render_office_document

    converter = tmp_path / 'converter'
    converter.write_text(f'#!{sys.executable}\n' + '''import sys
from pathlib import Path
import fitz
source = Path(sys.argv[-1])
with fitz.open() as document:
    document.new_page().insert_text((20, 20), 'Converted first page')
    document.new_page().insert_text((20, 20), 'Converted second page')
    document.save(source.with_suffix('.pdf'))
''')
    converter.chmod(0o700)
    monkeypatch.setenv('OA_LIBREOFFICE_BIN', str(converter))
    source = tmp_path / 'invoice.doc'
    source.write_bytes(b'recorded DOC fixture')
    output = tmp_path / 'prepared'
    output.mkdir()
    pages, count, texts = render_office_document(source, out_dir=output, resource_id='doc-1')
    assert count == 2
    assert len(pages) == 2
    assert texts == ['P1: Converted first page', 'P2: Converted second page']
    assert source.read_bytes() == b'recorded DOC fixture'


def test_zip_member_cannot_escape_output_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    from apps.oa.materials.archives import extract_zip_safely

    archive = tmp_path / 'unsafe.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('../../escaped.txt', 'not allowed')
    children, error = extract_zip_safely(archive, dest_dir=tmp_path / 'downloads',
                                        resource={'id': 'unsafe', 'name': archive.name})
    assert children == []
    assert '路径不安全' in error
    assert not (tmp_path / 'escaped.txt').exists()


@pytest.mark.parametrize('changed', ['material', 'image'])
def test_deepseek_does_not_report_complete_when_input_changes(tmp_path, monkeypatch, changed):
    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-auth.env'))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-api-key')
    from apps.oa.materials import recognize as attachment_recognition

    image = tmp_path / 'invoice.png'
    Image.new('RGB', (12, 8), 'white').save(image)
    material = tmp_path / 'material.json'
    material.write_text(json.dumps({'request_id': '12', 'prepared': {
        'images': [{'resource_id': 'r1', 'path': str(image),
                    'content_sha256': hashlib.sha256(image.read_bytes()).hexdigest()}],
        'manifest': [{'resource_id': 'r1', 'status': 'image', 'image_count': 1}],
        'structured_contents': [],
    }}))

    def post(*_args, **_kwargs):
        if changed == 'material':
            material.write_text(material.read_text() + '\n')
        else:
            Image.new('RGB', (12, 8), 'red').save(image)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '25.00'}}]}

    monkeypatch.setattr(attachment_recognition, '_post', post)
    result = attachment_recognition.recognize(material_file=material, output_dir=tmp_path / 'report')
    assert result['status'] == 'failed'
    assert result['error_code'] == ('frozen_material_changed' if changed == 'material' else 'attachment_page_changed')
