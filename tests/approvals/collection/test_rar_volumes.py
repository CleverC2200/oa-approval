from collections import Counter
import json
from pathlib import Path
import shutil

import pytest

from apps.oa.approvals.client import query
from apps.oa.approvals.collection import downloads
from apps.oa.approvals.collection.case import CollectionFailure
from apps.oa.approvals.demo import create_demo
from apps.oa.approvals.sources.download_errors import Downloaded
from apps.oa.materials.resources import deterministic_filename
from tests.approvals.source_fakes import POLICY, download_sources
from tests.rar_fixtures import FIRST, LAST


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
def test_collect_isolates_missing_rar_volume_then_recovers_using_fresh_originals(tmp_path, monkeypatch):
    app = create_demo(tmp_path)
    app.collector = None
    app.source_policy = POLICY
    first_row = next(row for row in app.api.rows if row['workflow_id'] == 'demo-general')
    first_row['received_at'] = '2026-10-22 09:59:00'
    remote = {'101': FIRST, '201': b'<Invoice><Amount>8.25</Amount></Invoice>'}
    calls = []

    def context(request_id):
        workflow = next(row for row in app.api.rows if row['request_id'] == request_id)
        names = ({key: name for key, name in [('101', 'evidence.part1.rar'), ('102', 'evidence.part2.rar')]
                  if key in remote} if workflow is first_row else {'201': 'normal.xml'})
        links = ''.join(
            f'<a href="/news/download.do?fileid={resource_id}&amp;filename={name}">附件</a>'
            for resource_id, name in names.items()
        )
        return {
            'requestId': request_id, 'workflowBaseInfo': {
                'workflowId': workflow['workflow_id'], 'workflowName': workflow['workflow_name'],
            },
            'workflowMainTableInfo': {'requestRecords': [{'workflowRequestTableFields': [
                {'fieldShowName': '附件', 'filedHtmlShow': links},
            ]}]},
        }, [{'id': resource_id, 'name': name, 'downloadUrl': f'/files/{resource_id}'}
            for resource_id, name in names.items()]

    def recorded_download(resource, *, dest_dir, **_options):
        path = dest_dir / deterministic_filename(resource)
        path.write_bytes(remote[resource['id']])
        calls.append(resource['id'])
        return Downloaded(path)

    app.api.request_context = context
    monkeypatch.setattr(downloads, 'build_download_sources', lambda _policy: download_sources(recorded_download))

    run = app.pending(workflows=['general_expense', 'business_entertainment'], batch_size=2)
    task, normal = run['tasks']
    assert task['workflow_key'] == 'general_expense'
    with pytest.raises(CollectionFailure, match='^attachment_archive_failed$') as failed:
        app.collect(run['run_id'], expected_task_id=task['id'])
    assert failed.value.diagnostics == [
        {'stage': 'archive_expand', 'reason': 'rar_volume_missing', 'attachment_ref': '101'},
    ]
    report = json.loads(Path(failed.value.diagnostics_file).read_text())
    original = next(Path(report['material_directory']).rglob('101_evidence.part1.rar'))
    assert original.read_bytes() == FIRST
    status = query(tmp_path, 'status', run_id=run['run_id'], view='batch')
    assert [item['request_id'] for item in status['recovery_candidates']] == [task['request_id']]
    assert app.inspect(task['id'])['task']['status'] == 'failed'
    collected_normal = app.collect(run['run_id'], expected_task_id=normal['id'])
    assert collected_normal['status'] == 'prepared'
    normal_material = json.loads(Path(collected_normal['material_file']).read_text())
    assert any('8.25' in line for line in normal_material['prepared']['structured_contents'][0]['key_text'])
    app.fail(run['run_id'], normal['id'], 'synthetic_test_finished')
    app.close(run['run_id'])

    remote['102'] = LAST
    recovery = app.pending(workflows=['general_expense'], batch_size=1,
                           recovery_request_ids=[task['request_id']])
    result = app.collect(recovery['run_id'], expected_task_id=task['id'])
    assert result['status'] == 'prepared'
    material = json.loads(Path(result['material_file']).read_text())
    first, last = material['resources']
    assert [item['resource_id'] for item in (first, last)] == ['101', '102']
    assert first['archive_volume_set'] == last['archive_volume_set'] == {
        'backend': '7zz', 'format': 'Rar5', 'volume_resource_ids': ['101', '102'],
    }
    assert all(item['source_references'][0]['source_field_path'] == 'main[0].field[0]'
               for item in (first, last))
    original_paths = [Path(item['local_readonly_path']) for item in (first, last)]
    assert [path.read_bytes() for path in original_paths] == [FIRST, LAST]
    assert original_paths[0] != original and original.read_bytes() == FIRST
    assert material['prepared']['structured_contents'][0]['parent_resource_id'] == '101'
    assert Counter(calls) == {'101': 2, '102': 1, '201': 1}
    dispatch = app.dispatch_review(recovery['run_id'], expected_task_id=task['id'],
                                   agent_id='synthetic-reviewer', agent_workflow_key=task['workflow_key'])
    assert dispatch['status'] == 'reviewing'
    assert app.api.handled == []
