from pathlib import Path

from PIL import Image

from apps.oa.approvals.collection import case, downloads
from tests.approvals.collection.test_case import FLOW_CONFIGS, RecordedOaApi
from tests.approvals.source_fakes import POLICY


def test_packaging_collection_preserves_same_field_images_and_records_cross_field_duplicates(tmp_path, monkeypatch):
    config = next(item for item in FLOW_CONFIGS if item['flow_key'] == 'packaging_layout_approval')

    class Api(RecordedOaApi):
        def request_context(self, request_id):
            workflow, _ = super().request_context(request_id)
            workflow['requestId'] = request_id
            workflow['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields'] = [
                {'fieldName': 'xcfj', 'fieldShowName': '宣传附件（此处请上传卖点证明材料）',
                 'filedHtmlShow': ''.join(f'<a href="/news/download.do?fileid={key}&filename=版面.png">版面</a>'
                                        for key in ['101', '102'])},
                {'fieldName': 'cpxxb', 'fieldShowName': '产品信息表',
                 'filedHtmlShow': '<a href="/news/download.do?fileid=201&filename=版面.png">版面</a>'},
            ]
            return workflow, []

    targets = []

    def download(resources, **kwargs):
        targets.extend(resources)
        items = []
        for resource in resources:
            path = tmp_path / (resource['id'] + '.png')
            Image.new('RGB', (40, 20), 'white').save(path)
            items.append({'resource_id': resource['id'], 'name': resource['name'], 'path': str(path)})
        return downloads.DownloadResult(tuple(items), (), 'complete')

    monkeypatch.setattr(downloads, 'download_resources', download)
    material = case.collect_case('12', oa_api=Api(config['workflow_id'], config['workflow_name']),
                                 configs=[config], source_policy=POLICY, allow_browser_download=False,
                                 attachment_root=tmp_path / 'attachments', prepared_root=tmp_path / 'prepared')

    assert [item['id'] for item in targets] == ['101', '102']
    assert [item['resource_id'] for item in material['resources']] == ['101', '102']
    assert all(item['review_role'] == 'packaging_image' for item in material['resources'])
    assert {item['resource_id'] for item in material['prepared']['images']} == {'101', '102'}
    assert all(Path(item['path']).is_file() for item in material['prepared']['images'])
    assert material['attachment_deduplication'] == [{
        'reason': 'cross_field_duplicate_filename', 'name': '版面.png',
        'kept_source_resource_id': '101', 'dropped_source_resource_id': '201',
    }]
