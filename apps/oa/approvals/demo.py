from __future__ import annotations

import hashlib
from pathlib import Path


CONFIGS = [
    {'flow_key': key, 'workflow_id': identifier, 'workflow_name': name}
    for key, identifier, name in [
        ('travel_expense', 'demo-travel', '合成差旅'),
        ('business_entertainment', 'demo-entertainment', '合成招待'),
        ('general_expense', 'demo-general', '合成通用费用'),
    ]
]


class DemoApi:
    def __init__(self):
        self.rows = [
            {'request_id': str(90001 + index), 'workflow_id': config['workflow_id'],
             'workflow_name': config['workflow_name'], 'operator_id': 'demo',
             'received_at': f'2026-10-22 10:0{index}:00', 'request_name': '合成测试单据'}
            for index, config in enumerate(CONFIGS)
        ]
        self.handled = []

    def pending_rows(self, **_options):
        return [dict(row) for row in self.rows]

    def handled_rows(self, **_options):
        return [dict(row) for row in self.handled]

    def submit_and_verify(self, request_id, remark):
        row = next(row for row in self.rows if row['request_id'] == request_id)
        self.rows.remove(row)
        self.handled.append(row)
        return {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True, 'failure_code': None}


def collect_demo(request_id, *, oa_api, configs, attachment_root, prepared_root, **_options):
    import fitz

    row = next(row for row in oa_api.rows if row['request_id'] == request_id)
    config = next(config for config in configs if config['workflow_id'] == row['workflow_id'])
    source = Path(attachment_root) / 'demo.pdf'
    source.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((30, 40), 'Synthetic fixture: total 12.50 CNY; not a real invoice.')
        document.save(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    from apps.oa.materials.formats import prepare_materials
    images, manifest, structured = prepare_materials(
        [{'path': str(source), 'resource_id': 'demo-pdf', 'name': 'demo.pdf'}], out_dir=Path(prepared_root))
    return {
        'schema_version': 1, 'kind': 'preaudit_material', 'request_id': request_id,
        'workflow_key': config['flow_key'], 'workflow_name': config['workflow_name'],
        'declared_fields': [{'path': 'main[0].field[0]', 'name': '测试金额', 'value': '12.50',
                             'read_status': 'present', 'value_source': 'raw'}],
        'resources': [{'resource_id': 'demo-pdf', 'name': 'demo.pdf', 'local_readonly_path': str(source),
                       'source_content_sha256': digest, 'source_field_path': 'main[0].field[1]'}],
        'prepared': {'images': images, 'manifest': manifest, 'structured_contents': structured},
        'authority_facts': [], 'warnings': [],
    }


def create_demo(root):
    from apps.oa.approvals.operations import Approval
    api = DemoApi()
    return Approval(root, api=api, configs=CONFIGS, collector=collect_demo, writer=api)
