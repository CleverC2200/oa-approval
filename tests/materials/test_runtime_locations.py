from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.oa.approvals.operations import Approval
from apps.oa.approvals.collection import cache as attachment_cache
from apps.oa.materials import recognize as attachment_recognition
from apps.oa.approvals.collection import downloads as attachments


def test_collection_cache_follows_runtime_root_across_working_directories(tmp_path, monkeypatch):
    monkeypatch.delenv('OA_FINANCE_PREAUDIT_CACHE_DB')
    calls = []

    def download(resource, *, dest_dir, **_options):
        path = dest_dir / 'source.txt'
        path.write_text('synthetic source')
        calls.append(path)
        return path, ''

    monkeypatch.setattr(attachments, '_download_resource_direct', download)

    def collect(request_id, *, attachment_root, **_options):
        files, errors = attachments.download_resources(
            [{'id': 'source', 'name': 'source.txt', 'downloadUrl': '/download?fileid=source'}],
            dest_dir=attachment_root, timeout_sec=1, allow_browser_download=True,
            request_id=request_id, flow_key='general_expense', reuse_cache=True)
        assert errors == []
        return {'request_id': request_id, 'workflow_key': 'general_expense',
                'resources': [{'resource_id': 'source', 'path': files[0]['path']}],
                'prepared': {'images': [], 'structured_contents': [], 'manifest': []}}

    roots = [tmp_path / 'first', tmp_path / 'second', tmp_path / 'first']
    for index, root in enumerate(roots):
        cwd = tmp_path / f'cwd-{index}'
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        api = SimpleNamespace(pending_rows=lambda: [{
            'request_id': '101', 'received_at': f'2026-10-24 10:0{index}:00',
            'operator_id': 'synthetic', 'workflow_key': 'general_expense'}])
        approval = Approval(root, api=api, configs=[], collector=collect)
        try:
            run = approval.pending()
            task = next(item for item in run['tasks'] if item['status'] == 'queued')
            result = approval.collect(run['run_id'], agent_id=f'agent-{index}',
                expected_task_id=task['id'], agent_workflow_key=task['workflow_key'])
            assert result['status'] == 'reviewing'
            assert Path(result['material_file']).is_relative_to(root)
            assert (root / 'attachment-cache.sqlite3').is_file()
            assert not (cwd / 'runtime').exists()
        finally:
            approval.store.connection.close()
    assert len(calls) == 3
    assert 'OA_FINANCE_PREAUDIT_CACHE_DB' not in os.environ


def test_material_recognition_defaults_to_unique_version_local_directories(tmp_path, monkeypatch):
    directory = tmp_path / 'selected-root' / 'materials' / '7' / 'version'
    directory.mkdir(parents=True)
    material_file = directory / 'material.json'
    material_file.write_text(json.dumps({'request_id': '101', 'prepared': {
        'images': [], 'manifest': [],
        'structured_contents': [{'resource_id': 'source', 'text': 'synthetic amount 12.50'}]}}))
    original = material_file.read_bytes()
    reports = []
    for name in ('first-cwd', 'second-cwd'):
        cwd = tmp_path / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        report = attachment_recognition.recognize(material_file=material_file)
        assert report['status'] == 'complete'
        report_file = Path(report['report_file'])
        assert report_file.parent.parent == directory / 'recognitions'
        assert json.loads(report_file.read_text()) == report
        reports.append(report_file)
    assert reports[0] != reports[1]
    explicit = tmp_path / 'explicit-output'
    report = attachment_recognition.recognize(material_file=material_file, output_dir=explicit)
    assert Path(report['report_file']).parent == explicit
    assert material_file.read_bytes() == original


@pytest.mark.parametrize('workflow', ['general_expense', 'overseas_invoice'])
@pytest.mark.parametrize('cache_setting', ['default', 'shared', 'off'])
def test_collection_preserves_cache_overrides_and_restores_context(
    tmp_path, monkeypatch, workflow, cache_setting,
):
    from apps.oa.approvals.collection import overseas
    from apps.oa.runtime import control as runtime_control

    root = tmp_path / 'selected-root'
    shared = tmp_path / 'shared' / 'cache.sqlite3'
    if cache_setting == 'default':
        monkeypatch.delenv('OA_FINANCE_PREAUDIT_CACHE_DB')
    else:
        monkeypatch.setenv('OA_FINANCE_PREAUDIT_CACHE_DB', str(shared) if cache_setting == 'shared' else 'off')
    previous = os.environ.get('OA_FINANCE_PREAUDIT_CACHE_DB')
    receipts = []

    def collect(request_id, **_options):
        receipts.append(attachment_cache.record_attachment_file(
            request_id=request_id, flow_key=workflow, resource_key='source',
            entry_json={'path': 'synthetic.txt'}))
        return {'request_id': request_id, 'workflow_key': workflow, 'resources': [],
                'prepared': {'images': [], 'structured_contents': [], 'manifest': []}}

    monkeypatch.setattr(overseas, 'collect', lambda _api, request_id, _directory: collect(request_id))
    api = SimpleNamespace(pending_rows=lambda: [{
        'request_id': '101', 'received_at': '2026-10-24 10:00:00',
        'operator_id': 'synthetic', 'workflow_key': workflow}])
    approval = Approval(root, api=api, configs=[], collector=collect)
    try:
        run = approval.pending()
        result = approval.collect(run['run_id'], agent_id='synthetic-agent',
            expected_task_id=run['tasks'][0]['id'], agent_workflow_key=workflow)
        assert result['status'] == 'reviewing'
        assert receipts == [{'enabled': cache_setting != 'off', 'ok': True}]
        assert (root / 'attachment-cache.sqlite3').exists() == (cache_setting == 'default')
        assert shared.exists() == (cache_setting == 'shared')
        assert os.environ.get('OA_FINANCE_PREAUDIT_CACHE_DB') == previous
        if cache_setting == 'default':
            assert attachment_cache.db_path() == runtime_control.RUNTIME_ROOT / 'attachment-cache.sqlite3'
    finally:
        approval.store.connection.close()


def test_reference_import_default_is_anchored_outside_working_directory(tmp_path, monkeypatch):
    import sys

    from openpyxl import Workbook

    from apps.oa.runtime import control as runtime_control
    from apps.oa.references import tpm as tpm_price_reference

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '参考报价单'
    sheet.append(['序号', '类型', '材料名称', '材料编码', '材质说明/要求', '单位', '未税指导价（元）', '含税指导价（元，1%）'])
    sheet.append([1, 'synthetic', '测试板', '001', '5mm', '㎡', 99, 20])
    source = tmp_path / 'reference.xlsx'
    workbook.save(source)
    workbook.close()
    runtime = tmp_path / 'project' / 'runtime'
    monkeypatch.setattr(runtime_control, 'RUNTIME_ROOT', runtime)
    cwd = tmp_path / 'unrelated-cwd'
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, 'argv', ['tpm-price-reference', str(source)])

    tpm_price_reference.main()

    catalog = tpm_price_reference.snapshot(runtime / 'reference' / 'tpm-price-reference.json')
    assert catalog['status'] == 'ready'
    assert catalog['rows'][0]['material_code'] == '001'
    assert not (cwd / 'runtime').exists()
