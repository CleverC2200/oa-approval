from __future__ import annotations

from tests.approvals.source_fakes import POLICY, download_sources
from apps.oa.approvals.sources.download_errors import Downloaded, Unavailable

from collections import Counter
import io
from pathlib import Path
from threading import Lock
import zipfile

import openpyxl
from openpyxl.drawing.image import Image as WorkbookImage
from PIL import Image
import pytest

from apps.oa.approvals.collection import cache as attachment_cache
from apps.oa.materials import formats as attachment_extractors
from apps.oa.approvals.collection import downloads as attachments
from apps.oa.materials import resources as material_resources
from apps.oa.materials import spreadsheet_images
from apps.oa.approvals.collection import case as case_bundle


@pytest.fixture
def png_bytes():
    picture = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(picture, format='PNG')
    return picture.getvalue()


@pytest.fixture
def workbook_bytes(png_bytes):
    workbook = openpyxl.Workbook()
    workbook.active['A1'] = 'recorded amount'
    workbook.active.add_image(WorkbookImage(io.BytesIO(png_bytes)), 'B2')
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


@pytest.fixture
def recorded_download(tmp_path, monkeypatch, workbook_bytes):
    calls = []
    lock = Lock()
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CACHE_DB', str(tmp_path/'cache.sqlite3'))

    def download(resource, *, dest_dir, **_kwargs):
        path = dest_dir / material_resources.deterministic_filename(resource)
        path.write_bytes(workbook_bytes + (b'changed' if resource['id'] == 'different' else b''))
        with lock:
            calls.append(resource['id'])
        return Downloaded(path)

    sources = download_sources(download)
    monkeypatch.setattr(attachments, 'build_download_sources', lambda _policy: sources)
    return calls


def resources(ids):
    return [{'id':rid, 'name':'recorded.xlsx', 'downloadUrl':f'/download?fileid={rid}'} for rid in ids]


@pytest.fixture
def collect_attachments(tmp_path, monkeypatch):
    def collect(*, detail_fields, logs, listed, originals, request_id='recorded', invoice_title_reader=None):
        class RecordedOaApi:
            def request_context(self, requested_id):
                assert requested_id == request_id
                return {
                    'requestId': request_id,
                    'workflowBaseInfo': {'workflowId': '195021', 'workflowName': '通用费用报销申请流程'},
                    'workflowDetailTableInfos': [{'workflowRequestTableRecords': [
                        {'workflowRequestTableFields': [
                            {'fieldShowName': '附件', **(field if isinstance(field, dict) else {'filedHtmlShow': field})},
                        ]}
                        for field in detail_fields
                    ]}],
                    'workflowRequestLogs': [{'annexDocHtmls': log} for log in logs],
                }, listed

        def download(resource, *, dest_dir, **_kwargs):
            path = dest_dir / material_resources.deterministic_filename(resource)
            path.write_bytes(originals[resource['id']])
            calls.append((resource['_workflow_request_id'], resource['id']))
            return Downloaded(path)

        monkeypatch.setattr(attachments, 'build_download_sources', lambda _policy: download_sources(download))
        return case_bundle.collect_case(
            request_id, oa_api=RecordedOaApi(), configs=[{
                'flow_key': 'general_expense', 'workflow_id': '195021', 'workflow_name': '通用费用报销申请流程',
            }], allow_browser_download=True, attachment_root=tmp_path / 'downloads',
            prepared_root=tmp_path / 'prepared', source_policy=POLICY, invoice_title_reader=invoice_title_reader,
        )

    calls = []
    return collect, calls


def attachment_link(resource_id):
    return f'<a href="/news/download.do?fileid={resource_id}&amp;filename=recorded.xlsx">附件</a>'


@pytest.mark.parametrize('signature_amount', ['recorded amount', 'signed amount'], ids=['same', 'different'])
def test_collection_preserves_signature_and_detail_originals(
    collect_attachments, workbook_bytes, signature_amount,
):
    collect, calls = collect_attachments
    detail_id, signature_id = '101', '102'
    detail_path = 'detail[0].row[0].field[0]'
    signature_path = 'workflowRequestLogs[0].annexDocHtmls'
    signature_bytes = workbook_bytes
    if signature_amount != 'recorded amount':
        signature_workbook = openpyxl.load_workbook(io.BytesIO(workbook_bytes))
        signature_workbook.active['A1'] = signature_amount
        signature_file = io.BytesIO()
        signature_workbook.save(signature_file)
        signature_workbook.close()
        signature_bytes = signature_file.getvalue()
    originals = {detail_id: workbook_bytes, signature_id: signature_bytes}
    material = collect(
        detail_fields=[attachment_link(detail_id)], logs=[attachment_link(signature_id)],
        listed=resources([detail_id, signature_id]), originals=originals,
    )

    assert Counter(calls) == Counter([('recorded', detail_id), ('recorded', signature_id)])
    detail, signature = material['resources']
    assert (detail['source_resource_id'], detail['source_field_path'], detail['owner_detail_id']) == (
        detail_id, detail_path, 'detail[0].row[0]',
    )
    assert (signature['source_resource_id'], signature['source_field_path'], signature['owner_detail_id']) == (
        signature_id, signature_path, None,
    )
    detail_original, signature_original = (Path(item['local_readonly_path']) for item in material['resources'])
    assert detail_original != signature_original
    assert detail_original.read_bytes() == workbook_bytes
    assert signature_original.read_bytes() == originals[signature_id]
    detail_body, signature_body = material['prepared']['structured_contents']
    assert detail_body['resource_id'] == detail_id
    assert 'Sheet!R1: C1=recorded amount' in detail_body['key_text']
    assert signature_body['resource_id'] == signature_id
    assert f'Sheet!R1: C1={signature_amount}' in signature_body['key_text']
    assert detail_body['source_path'] == str(detail_original)
    assert signature_body['source_path'] == str(signature_original)
    detail_image, signature_image = material['prepared']['images']
    assert (detail_image['path'] == signature_image['path']) == (signature_amount == 'recorded amount')


def test_collection_keeps_originals_when_two_ids_yield_the_same_download_filename(collect_attachments):
    collect, calls = collect_attachments
    original_name = '101_1_evidence.xml'
    originals = {
        '101': b'<Invoice><Amount>73.12</Amount></Invoice>',
        '101_1': b'<Invoice><Amount>28.46</Amount></Invoice>',
    }
    listed = [{**resource, 'name': original_name} for resource in resources(originals)]
    links = [attachment_link(resource_id).replace('recorded.xlsx', original_name) for resource_id in originals]

    material = collect(detail_fields=links[:1], logs=links[1:], listed=listed, originals=originals)

    assert Counter(calls) == Counter(('recorded', resource_id) for resource_id in originals)
    paths = {item['resource_id']: Path(item['local_readonly_path']) for item in material['resources']}
    assert len(set(paths.values())) == 2
    assert {resource_id: path.read_bytes() for resource_id, path in paths.items()} == originals
    contents = {item['resource_id']: item for item in material['prepared']['structured_contents']}
    for resource_id, amount in [('101', '73.12'), ('101_1', '28.46')]:
        assert contents[resource_id]['source_path'] == str(paths[resource_id])
        assert any(amount in line for line in contents[resource_id]['key_text'])


@pytest.mark.parametrize('included_in_list', [False, True], ids=['html-only', 'listed'])
def test_collection_preserves_each_verified_reference_to_same_id(collect_attachments, workbook_bytes, included_in_list):
    collect, calls = collect_attachments
    attachment = attachment_link('101')
    listed = resources(['101'])
    listed[0]['_trusted_source_references'] = [{'source_field_path': 'injected'}]
    material = collect(
        detail_fields=[attachment, attachment.replace('/news', 'https://external.invalid/news')],
        logs=[attachment, attachment + attachment], listed=listed if included_in_list else [],
        originals={'101': workbook_bytes},
    )

    assert calls == [('recorded', '101')]
    resource, = material['resources']
    assert resource['owner_detail_id'] == 'detail[0].row[0]'
    assert resource['source_field_path'] == ('detail[0].row[0].field[0]' if included_in_list else '')
    assert resource['source_references'] == [
        {'source_field_path': 'detail[0].row[0].field[0]', 'source_field_name': '附件',
         'owner_detail_id': 'detail[0].row[0]'},
        {'source_field_path': 'workflowRequestLogs[0].annexDocHtmls', 'source_field_name': '签字附件',
         'owner_detail_id': None},
        {'source_field_path': 'workflowRequestLogs[1].annexDocHtmls', 'source_field_name': '签字附件',
         'owner_detail_id': None},
    ]
    assert Path(resource['local_readonly_path']).read_bytes() == workbook_bytes


def test_collection_rejects_conflicting_download_addresses_for_the_same_id(collect_attachments):
    collect, calls = collect_attachments
    listed = resources(['101', '101'])
    listed[1]['downloadUrl'] = '/download?fileid=102'
    with pytest.raises(RuntimeError, match='^attachment_resource_identity_conflict$'):
        collect(detail_fields=[attachment_link('101')], logs=[attachment_link('101')], listed=listed, originals={})
    assert calls == []


def test_collection_does_not_choose_an_owner_between_distinct_detail_references(collect_attachments, workbook_bytes):
    collect, calls = collect_attachments
    attachment = attachment_link('101')
    material = collect(detail_fields=[attachment, attachment], logs=[attachment], listed=[],
                       originals={'101': workbook_bytes})

    assert calls == [('recorded', '101')]
    resource, = material['resources']
    assert resource['owner_detail_id'] is None
    assert [source['owner_detail_id'] for source in resource['source_references']] == [
        'detail[0].row[0]', 'detail[0].row[1]', None,
    ]


@pytest.mark.parametrize('listed', [False, True], ids=['discovered', 'already-listed'])
def test_collection_preserves_verified_travel_field_references(collect_attachments, workbook_bytes, listed):
    collect, calls = collect_attachments
    url = 'https://trip.example.test/opera/getFile/101/recorded.xlsx'
    alias = 'https://trip.example.test/opera/share/open/101/recorded.xlsx'
    raw_id = 'listed-101' if listed else 'field_detail_0_0_1'
    material = collect(
        detail_fields=[
            {'fieldValue': url, 'fieldShowValue': url}, {'fieldValue': url}, {'fieldValue': alias},
            {'fieldValue': url.replace('trip.example.test', 'external.invalid')},
        ], logs=[], originals={raw_id: workbook_bytes},
        listed=[{'id': raw_id, 'name': 'recorded.xlsx', 'downloadUrl': url,
                 'allowExtraDownloadHost': True}] if listed else [],
    )

    assert calls == [('recorded', raw_id)]
    resource, = material['resources']
    assert (resource['resource_id'], resource['source_field_path'], resource['owner_detail_id']) == (
        (raw_id, '', None) if listed else ('trip_file:101', 'detail[0].row[0].field[0]', 'detail[0].row[0]')
    )
    assert resource['source_references'] == [
        {'source_field_path': f'detail[0].row[{row}].field[0]', 'source_field_name': '附件',
         'owner_detail_id': f'detail[0].row[{row}]'} for row in range(3)
    ]
    assert Path(resource['local_readonly_path']).read_bytes() == workbook_bytes


def test_collection_obtains_original_and_authority_for_each_case(collect_attachments, workbook_bytes):
    collect, calls = collect_attachments
    authority_reads = []

    def invoice_title_reader():
        title = f'Recorded invoice title {len(authority_reads) + 1}'
        authority_reads.append(title)
        return {'status': 'ready', 'rows': [{'title': title}]}

    materials = [collect(detail_fields=[attachment_link('101')], logs=[], listed=resources(['101']),
                         originals={'101': workbook_bytes}, request_id=request_id,
                         invoice_title_reader=invoice_title_reader) for request_id in ('901', '902')]
    assert calls == [('901', '101'), ('902', '101')]
    assert [material['invoice_title_reference']['rows'] for material in materials] == [
        [{'title': 'Recorded invoice title 1'}], [{'title': 'Recorded invoice title 2'}],
    ]
    originals = [Path(material['resources'][0]['local_readonly_path']) for material in materials]
    assert originals[0] != originals[1]
    assert all(path.read_bytes() == workbook_bytes for path in originals)


@pytest.mark.parametrize('browser_fallback', [False, True])
def test_download_keeps_each_original_even_with_identical_content(
    tmp_path, recorded_download, workbook_bytes, browser_fallback,
):
    if browser_fallback:
        transfer = attachments.build_download_sources(POLICY).transfer
        transfer.browser = transfer.direct
        transfer.direct = lambda *_a, **_k: Unavailable()
    download_result = attachments.download_resources(
        resources(['first', 'second', 'different']), dest_dir=tmp_path/'downloads',
        timeout_sec=1, allow_browser_download=True, request_id='recorded', flow_key='tpm_small_marketing_fee',
     sources=attachments.build_download_sources(POLICY))
    files = list(download_result.items)
    errors = [error.to_record() for error in download_result.errors]
    assert errors == []
    assert Counter(recorded_download) == Counter(['first', 'second', 'different'])
    assert [item['resource_id'] for item in files] == ['first', 'second', 'different']
    paths = [Path(item['path']) for item in files]
    assert set(paths) == set((tmp_path/'downloads').rglob('*.xlsx'))
    assert len(set(paths)) == 3
    assert [path.read_bytes() for path in paths] == [workbook_bytes, workbook_bytes, workbook_bytes + b'changed']


def test_explicit_cached_content_reuse_preserves_previous_files(tmp_path, recorded_download, workbook_bytes):
    previous = tmp_path/'previous'
    previous.mkdir()
    originals = [previous/f'{rid}.xlsx' for rid in ('first', 'second')]
    for resource, path in zip(resources(['first', 'second']), originals):
        path.write_bytes(workbook_bytes)
        attachment_cache.record_attachment_file(
            request_id='recorded', flow_key='tpm_small_marketing_fee', resource_key=resource['id'],
            entry_json={'path':str(path), 'resource_id':resource['id'], 'name':resource['name']},
        )
    download_result = attachments.download_resources(
        resources(['first', 'second']), dest_dir=tmp_path/'next', timeout_sec=1,
        allow_browser_download=True, request_id='recorded', flow_key='tpm_small_marketing_fee', reuse_cache=True,
     sources=attachments.build_download_sources(POLICY))
    files = list(download_result.items)
    errors = [error.to_record() for error in download_result.errors]
    assert errors == []
    assert recorded_download == []
    assert all(path.read_bytes() == workbook_bytes for path in originals)
    assert [item['resource_id'] for item in files] == ['first', 'second']
    assert all(item['reused'] is True for item in files)


def test_download_preserves_a_preexisting_copy(tmp_path, recorded_download, workbook_bytes):
    destination = tmp_path / 'downloads'
    destination.mkdir()
    previous = destination / 'previously-collected.xlsx'
    previous.write_bytes(workbook_bytes)

    result = attachments.download_resources(
        resources(['first']), dest_dir=destination, timeout_sec=1, allow_browser_download=True,
        request_id='recorded', flow_key='tpm_small_marketing_fee',
        sources=attachments.build_download_sources(POLICY),
    )
    assert not result.errors
    assert recorded_download == ['first']
    original = Path(result.items[0]['path'])
    assert original != previous
    assert original.read_bytes() == previous.read_bytes() == workbook_bytes


def test_preparation_reuses_content_and_rebinds_each_resource(tmp_path, monkeypatch, workbook_bytes):
    items = []
    for rid, parent in [('first', 'archive-a'), ('second', 'archive-b')]:
        path = tmp_path/f'{rid}.xlsx'
        path.write_bytes(workbook_bytes)
        items.append({'resource_id':rid, 'parent_resource_id':parent, 'name':f'{rid}.xlsx', 'path':str(path)})
    calls = []
    extract = spreadsheet_images.extract

    def recorded_extract(*args, **kwargs):
        calls.append(args[0])
        return extract(*args, **kwargs)

    monkeypatch.setattr(spreadsheet_images, 'extract', recorded_extract)
    prepared_record = attachment_extractors.prepare_materials(items, out_dir=tmp_path/'prepared').to_record()
    images = prepared_record['images']
    manifest = prepared_record['manifest']
    facts = prepared_record['structured_contents']
    assert len(calls) == 1
    assert [(i['resource_id'], i['parent_resource_id'], i['name']) for i in images] == [
        ('first', 'archive-a', 'first.xlsx'), ('second', 'archive-b', 'second.xlsx'),
    ]
    assert len({str(i['path']) for i in images}) == 1
    assert len(list((tmp_path/'prepared').glob('*.png'))) == 1
    assert [i['resource_id'] for i in manifest] == ['first', 'second']
    assert [i['resource_id'] for i in facts] == ['first', 'second']
    assert all(i['spreadsheet_cell'] == 'Sheet!B2' for i in images)


def test_collection_preserves_67_originals_and_reuses_prepared_content(
    tmp_path, monkeypatch, recorded_download, workbook_bytes,
):
    ids = [str(1000 + index) for index in range(67)]

    class RecordedOaApi:
        def request_context(self, _request_id):
            rows = []
            position = 0
            for index in range(30):
                bound = ids[position:position + (3 if index < 7 else 2)]
                position += len(bound)
                rows.append({'workflowRequestTableFields':[{
                    'fieldName':'fj', 'fieldShowName':'附件', 'fieldValue':','.join(bound),
                    'filedHtmlShow':''.join(
                        f'<a href="/news/download.do?fileid={rid}&filename=recorded.xlsx">recorded.xlsx</a>'
                        for rid in bound
                    ),
                }]})
            return {
                'workflowBaseInfo':{'workflowId':'195039', 'workflowName':'TPM小额市场费用报销申请流程'},
                'workflowDetailTableInfos':[{'workflowRequestTableRecords':rows}],
            }, resources(ids)

    monkeypatch.setattr(case_bundle.invoice_buyers, 'read_invoice_buyers', lambda *_a, **_k:{
        'read_status':'not_applicable', 'invoices':[],
    })
    material = case_bundle.collect_case(
        'recorded', oa_api=RecordedOaApi(), configs=[{
            'flow_key':'tpm_small_marketing_fee', 'workflow_id':'195039',
            'workflow_name':'TPM小额市场费用报销申请流程',
        }], allow_browser_download=True, attachment_root=tmp_path/'downloads', prepared_root=tmp_path/'prepared',
     source_policy=POLICY)
    assert Counter(recorded_download) == Counter(ids)
    assert [r['resource_id'] for r in material['resources']] == ids
    assert Counter(r['owner_detail_id'] for r in material['resources']) == {
        f'detail[0].row[{i}]':3 if i < 7 else 2 for i in range(30)
    }
    originals = {Path(r['local_readonly_path']) for r in material['resources']}
    assert len(originals) == 67
    assert all(path.read_bytes() == workbook_bytes for path in originals)
    prepared = material['prepared']
    assert [i['resource_id'] for i in prepared['images']] == ids
    assert len({str(i['path']) for i in prepared['images']}) == 1
    assert len(list((tmp_path/'prepared').rglob('*.png'))) == 1
    assert [i['resource_id'] for i in prepared['manifest']] == ids
    assert [i['resource_id'] for i in prepared['structured_contents']] == ids


def test_identical_docx_files_keep_their_own_media_identities(tmp_path, png_bytes):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('word/document.xml', '<document><body/></document>')
        archive.writestr('word/media/image1.png', png_bytes)
        archive.writestr('word/media/image2.png', png_bytes)
    items = []
    for rid in ('first', 'second'):
        path = tmp_path/f'{rid}.docx'
        path.write_bytes(content.getvalue())
        items.append({'resource_id':rid, 'name':path.name, 'path':str(path)})
    prepared_record = attachment_extractors.prepare_materials(items, out_dir=tmp_path/'prepared').to_record()
    images = prepared_record['images']
    assert [(i['resource_id'], i['parent_resource_id']) for i in images] == [
        (f'{rid}::docx_media::{index}', rid) for rid in ('first', 'second') for index in (1, 2)
    ]
    assert all(i['name'].startswith(f"{i['parent_resource_id']}.docx::word/media/") for i in images)


def test_identical_archive_videos_keep_parent_in_skip_records(tmp_path):
    children = []
    for index in (1, 2):
        path = tmp_path/f'{index}.mp4'
        path.write_bytes(b'recorded unread video')
        children.append({'resource_id':f'archive::{index}', 'parent_resource_id':'archive', 'path':str(path)})
    prepared_record = attachment_extractors.prepare_materials(
        [{'resource_id':'archive', 'extracted_files':children}], out_dir=tmp_path/'prepared',
    ).to_record()
    images = prepared_record['images']
    manifest = prepared_record['manifest']
    assert images == []
    assert [(i['resource_id'], i['parent_resource_id']) for i in manifest[0]['skipped_videos']] == [
        ('archive::1', 'archive'), ('archive::2', 'archive'),
    ]


def test_identical_archive_image_stays_inside_prepared_directory(tmp_path, png_bytes):
    standalone = tmp_path/'standalone.png'
    child = tmp_path/'child.png'
    standalone.write_bytes(png_bytes)
    child.write_bytes(png_bytes)
    prepared = tmp_path/'prepared'
    prepared_record = attachment_extractors.prepare_materials([
        {'resource_id':'standalone', 'path':str(standalone)},
        {'resource_id':'archive', 'extracted_files':[
            {'resource_id':'archive::1', 'parent_resource_id':'archive', 'path':str(child)},
        ]},
    ], out_dir=prepared).to_record()
    images = prepared_record['images']
    assert images[0]['path'] == standalone
    assert Path(images[1]['path']).is_relative_to(prepared)
    assert images[1]['parent_resource_id'] == 'archive'
    assert Path(images[1]['path']).read_bytes() == png_bytes


def test_cached_archive_reuse_preserves_existing_children(tmp_path, monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as source:
        source.writestr('invoice.txt', 'original invoice')
    calls = []

    def download(resource, *, dest_dir, **kwargs):
        calls.append(resource['id'])
        path = dest_dir / 'source.zip'
        path.write_bytes(archive.getvalue())
        return Downloaded(path)

    sources = download_sources(download)
    monkeypatch.setattr(attachments, 'build_download_sources', lambda _policy: sources)
    target = [{'id': 'archive-one', 'name': 'source.zip', 'downloadUrl': '/source.zip'}]
    options = dict(dest_dir=tmp_path / 'files', request_id='101', flow_key='general_expense',
                   timeout_sec=5, allow_browser_download=True, reuse_cache=True)
    download_result = attachments.download_resources(target, **options, sources=attachments.build_download_sources(POLICY))
    first = list(download_result.items)
    errors = [error.to_record() for error in download_result.errors]
    assert errors == []
    child = Path(first[0]['extracted_files'][0]['path'])
    retained = child.parent / 'retained.txt'
    retained.write_text('existing derived evidence')
    child.write_text('changed derived invoice')
    download_result = attachments.download_resources(target, **options, sources=attachments.build_download_sources(POLICY))
    second = list(download_result.items)
    errors = [error.to_record() for error in download_result.errors]
    assert errors == []
    assert calls == ['archive-one']
    fresh = second[0]['extracted_files'][0]
    assert fresh['resource_id'] == first[0]['extracted_files'][0]['resource_id']
    assert fresh['parent_resource_id'] == 'archive-one'
    assert Path(fresh['path']) != child
    assert Path(fresh['path']).read_text() == 'original invoice'
    assert child.read_text() == 'changed derived invoice'
    assert retained.read_text() == 'existing derived evidence'


def test_photo_archive_keeps_all_154_images_and_parent_identity(tmp_path, png_bytes):
    archive = tmp_path / 'photos.zip'
    with zipfile.ZipFile(archive, 'w') as source:
        for index in range(154):
            source.writestr(f'photos/{index}.png', png_bytes)
    original = archive.read_bytes()
    sources = download_sources(lambda *_a, **_kw: Downloaded(archive))
    result = attachments.download_resources(
        [{'id': 'photos', 'name': archive.name, 'downloadUrl': '/recorded/photos.zip'}],
        dest_dir=tmp_path / 'downloads', sources=sources, timeout_sec=5,
        allow_browser_download=True, request_id='recorded', flow_key='tpm_small_marketing_fee',
    )
    assert result.errors == ()
    children = result.items[0]['extracted_files']
    assert len(children) == 154
    assert len({child['resource_id'] for child in children}) == 154
    assert {child['parent_resource_id'] for child in children} == {'photos'}
    assert all(Path(child['path']).read_bytes() == png_bytes for child in children)
    assert archive.read_bytes() == original
