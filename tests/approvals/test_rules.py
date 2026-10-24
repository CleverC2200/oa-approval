from pathlib import Path
import re
import shutil

import pytest

from apps.oa.approvals.operations import Approval


@pytest.fixture
def rule_case(tmp_path, monkeypatch):
    def build(workflow_key):
        class RuleApi:
            def pending_rows(self):
                return [{'request_id': '101', 'workflow_key': workflow_key,
                         'operator_id': 'synthetic-reviewer', 'received_at': '2026-10-24 09:00:00'}]

            def request_context(self, request_id):
                from apps.oa.approvals import overseas as overseas_invoice

                fields = [{'fieldName': name, 'fieldValue': value, 'edit': True}
                          for name, value in [('scfp', '201'), ('fpje', '1'), ('fphm', ''), ('fpdqr', '')]]
                fields[0]['filedHtmlShow'] = '<a href="/news/download.do?fileid=201">invoice</a>'
                fields += [{'fieldName': name, 'fieldShowName': label, 'fieldValue': '', 'edit': True}
                           for name, label in [('date', '发票日期'), ('po', '发票采购订单号')]]
                return {'requestId': request_id, 'workflowId': overseas_invoice.WORKFLOW_ID,
                    'workflowName': overseas_invoice.WORKFLOW_NAME, 'currentNodeId': overseas_invoice.RPA_NODE_ID,
                    'canEdit': True, 'workflowMainTableInfo': {'requestRecords': [{
                        'workflowRequestTableFields': [{'fieldName': 'bz', 'fieldValue': 'USD'}]}]},
                    'workflowDetailTableInfos': [{'tableDBName': 'invoice_rows',
                        'workflowRequestTableRecords': [{'recordOrder': 1, 'workflowRequestTableFields': fields}]}]}, [
                    {'id': '201', 'name': 'synthetic.pdf', 'downloadUrl': '/news/download.do?fileid=201'}]

        if workflow_key == 'overseas_invoice':
            from apps.oa.approvals.collection import overseas

            def download(_resources, directory, **_options):
                directory.mkdir(parents=True)
                import fitz

                path = directory / 'synthetic.pdf'
                with fitz.open() as pdf:
                    pdf.new_page().insert_text((20, 20), 'Synthetic overseas invoice fixture.')
                    pdf.save(path)
                return [{'resource_id': '201', 'path': str(path), 'pages': 1}]

            monkeypatch.setattr(overseas, 'download_fresh', download)

        def collect(request_id, *, attachment_root, **_options):
            source = Path(attachment_root) / 'synthetic.txt'
            source.parent.mkdir(parents=True)
            source.write_text('Synthetic rule-package fixture.')
            return {'request_id': request_id, 'workflow_key': workflow_key,
                    'resources': [{'resource_id': 'synthetic', 'local_readonly_path': str(source)}],
                    'prepared': {'images': [], 'structured_contents': [], 'manifest': []}}

        app = Approval(tmp_path / 'runtime', api=RuleApi(), configs=[], collector=collect)
        started = app.pending()
        task = started['tasks'][0]
        return app, started['run_id'], {'agent_id': 'synthetic-reviewer',
            'expected_task_id': task['id'], 'agent_workflow_key': task['workflow_key']}
    return build


def test_collect_freezes_only_current_flow_rules(rule_case):
    app, run_id, guards = rule_case('travel_expense')
    try:
        collected = app.collect(run_id, **guards)
        root = Path(collected['rules_root'])
        assert {path.name for path in root.rglob('flows/*.md')} == {'travel-expense.md'}
        assert not (root / '.agents/skills/oa-finance-preaudit/SKILL.md').exists()
        assert not (root / 'README.md').exists()
        assert '| 唯一规则包 |' not in (Path(collected['rule_file']).parent.parent / 'result-contract.md').read_text()
    finally:
        app.store.connection.close()


@pytest.mark.parametrize('workflow_key', [
    'travel_expense', 'business_entertainment', 'business_support_fee',
    'small_marketing_fee', 'tpm_small_marketing_fee', 'general_expense',
    'packaging_layout_approval', 'overseas_invoice',
])
def test_frozen_rule_links_stay_inside_complete_current_package(rule_case, workflow_key):
    app, run_id, guards = rule_case(workflow_key)
    try:
        collected = app.collect(run_id, **guards)
        root = Path(collected['rules_root'])
        expected = set() if workflow_key == 'overseas_invoice' else {workflow_key.replace('_', '-') + '.md'}
        assert {path.name for path in root.rglob('flows/*.md')} == expected
        if workflow_key == 'overseas_invoice':
            assert {str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()} == {
                '.agents/skills/oa-overseas-invoice/SKILL.md', 'docs/overseas-invoice.md'}
        for document in root.rglob('*.md'):
            for link in re.findall(r'\]\(([^)]+)\)', document.read_text()):
                path = link.split('#', 1)[0]
                if path and '://' not in path:
                    target = (document.parent / path).resolve()
                    assert target.is_relative_to(root)
                    assert target.is_file(), (document.relative_to(root), link)
    finally:
        app.store.connection.close()


@pytest.mark.parametrize('missing', [
    '.agents/skills/oa-finance-preaudit/references/result-contract.md',
    '.agents/skills/audit-chinese-image-text/references/output-contract.md',
    'docs/overseas-invoice.md',
])
def test_missing_required_rules_fail_collection(rule_case, tmp_path, monkeypatch, missing):
    from apps.oa.approvals import rules as service

    project = tmp_path / 'source'
    shutil.copytree(service.PROJECT / '.agents', project / '.agents')
    shutil.copytree(service.PROJECT / 'docs', project / 'docs')
    (project / missing).unlink()
    monkeypatch.setattr(service, 'PROJECT', project)
    if 'audit-chinese' in missing:
        workflow = 'packaging_layout_approval'
    elif 'overseas' in missing:
        workflow = 'overseas_invoice'
    else:
        workflow = 'travel_expense'
    app, run_id, guards = rule_case(workflow)
    try:
        with pytest.raises(ValueError, match='rule_dependency_missing'):
            app.collect(run_id, **guards)
        task = app.inspect(guards['expected_task_id'])['task']
        assert task['status'] == 'failed'
        assert not task['material_file']
        assert not app.inspect(task['id'])['submissions']
    finally:
        app.store.connection.close()


def test_new_cross_flow_link_cannot_expand_or_leave_incomplete_rule_package(rule_case, tmp_path, monkeypatch):
    from apps.oa.approvals import rules as service

    project = tmp_path / 'source'
    shutil.copytree(service.PROJECT / '.agents', project / '.agents')
    shutil.copytree(service.PROJECT / 'docs', project / 'docs')
    contract = project / '.agents/skills/oa-finance-preaudit/references/result-contract.md'
    contract.write_text(contract.read_text() + '\n[Unrelated](flows/general-expense.md)\n')
    monkeypatch.setattr(service, 'PROJECT', project)
    app, run_id, guards = rule_case('travel_expense')
    try:
        with pytest.raises(ValueError, match='rule_dependency_out_of_scope'):
            app.collect(run_id, **guards)
        assert app.inspect(guards['expected_task_id'])['task']['status'] == 'failed'
    finally:
        app.store.connection.close()


def test_changed_frozen_rule_cannot_be_reviewed_or_submitted(rule_case):
    app, run_id, guards = rule_case('travel_expense')
    try:
        collected = app.collect(run_id, **guards)
        rule = Path(collected['rule_file'])
        rule.write_text(rule.read_text() + '\nChanged after collection.\n')
        with pytest.raises(ValueError, match='frozen_material_changed'):
            app.submit(collected['task_id'], opinion='Synthetic offline opinion.', confirm_write=True)
        details = app.inspect(collected['task_id'])
        assert not details['reviews']
        assert not details['submissions']
    finally:
        app.store.connection.close()
