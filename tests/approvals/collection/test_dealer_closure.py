from copy import deepcopy
import json
from pathlib import Path

import pytest

from apps.oa.approvals.operations import Approval
from apps.oa.approvals import flows
from apps.oa.approvals.sources import dealer_closure
from apps.oa.approvals.sources import invoices
from tests.approvals.source_fakes import POLICY
from tests.approvals.sources.test_dealer_closure import delivery, workflow


class ClosureApi:
    def __init__(self):
        self.rows = [{**delivery(), 'workflow_id': '195127', 'workflow_name': '经销商管理-关户'}]
        self.form = workflow()
        self.form['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields'].extend(
            [{'fieldName': code, 'fieldValue': ''} for code in ['fjsc1', 'fjsc2', 'tksqs']]
            + [{'fieldName': 'fj', 'fieldValue': 'https://crm.example.test/old-license.jpg', 'fieldShowName': '附件'}])
        self.form['workflowBaseInfo'] = {'workflowId': '195127', 'workflowName': '经销商管理-关户'}
        self.posts = []

    def pending_rows(self):
        return deepcopy(self.rows)

    def request_context(self, request_id):
        return deepcopy(self.form), []

    def submit_and_verify(self, request_id, opinion, *, dealer_material=None):
        kind = dealer_material['dealer_closure_evidence']['submission_context']['delivery_kind'] if dealer_material else 'approval'
        self.posts.append((request_id, opinion, kind))
        self.rows = []
        return {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}


@pytest.fixture
def closure_app(tmp_path, monkeypatch, request):
    actual = {'requestid': 101, 'currentnodeid': 30, 'currentnodetype': '3', 'nodename': '归档'}
    case = getattr(request, 'param', None)
    ai_empty = case in {'ai_empty', 'ai_empty_bi_empty'}
    if ai_empty:
        actual.update(currentnodeid=224785, currentnodetype='1', nodename='AI预审')
    monkeypatch.setattr(dealer_closure.official_sql, '_query_rows',
        lambda name, *_a, **_kw: ([{'currentnodeid': 224785, 'fieldname': code, 'isedit': None, 'ismandatory': None}
            for code in ('yetk', 'sftk')] if name == 'dealer_closure_branch_fields'
            else [{'operator_id': 9}] if name == 'dealer_closure_forward_log'
            else [{'remark': '合成历史意见', 'remark_length': 6}] if name == 'dealer_closure_history' else [actual.copy()]))
    monkeypatch.setattr(dealer_closure, 'settings', lambda: {})
    def worker(mode, payload, _config):
        if mode == 'doris':
            if case == 'ai_empty_bi_empty':
                return {'rows': []}
            return {'rows': [{'company_code': 'D001', 'cust_code': '99000001', 'cust_balance': '0.00',
                              'freezers_qty': None, 'ledger_account_control': None, 'pre_end_balance': '0.00'}]}
        return {'company_code': payload['company_code'], 'customer_code': payload['customer_code']}
    monkeypatch.setattr(dealer_closure, '_worker', worker)
    monkeypatch.setattr(dealer_closure, '_dms', lambda *_: {'response': {'success': True, 'result': None}})
    monkeypatch.setattr(invoices, 'read_invoice_buyers', lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError('expense source called')))
    api = ClosureApi()
    if ai_empty:
        api.form.update(currentNodeId='224785', submitButtonName='提交')
        api.rows[0].update(operator_node_id='224785', delivery_kind='approval')
        for field in api.form['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields']:
            if field['fieldName'] in {'yetk', 'sftk'}:
                field['fieldValue'] = ''
    app = Approval(tmp_path / 'runtime', api=api, writer=api, configs=flows.load_all_configs(), source_policy=POLICY)
    started = app.pending(workflows=['dealer_closure'])
    task = started['tasks'][0]
    collected = app.collect(started['run_id'], agent_id='closure-reviewer',
        expected_task_id=task['id'], agent_workflow_key='dealer_closure')
    yield app, api, collected
    app.store.connection.close()


def test_public_collect_freezes_closure_facts_and_uses_registered_feedback_writer(closure_app):
    app, api, collected = closure_app
    material = json.loads(Path(collected['material_file']).read_text())
    assert material['workflow_key'] == 'dealer_closure'
    assert material['dealer_closure_evidence']['bi']['rows'][0]['freezers_qty'] is None
    assert material['attachment_presence']['status'] == 'confirmed_empty'
    assert material['dealer_closure_evidence']['resource_inventory']['resource_count'] == 0
    assert material['dealer_closure_evidence']['history']['rows'][0]['remark'] == '合成历史意见'
    assert any(f['code'] == 'fj' and f['read_status'] != 'empty' for f in material['declared_fields'])
    assert Path(collected['rule_file']).name == 'SKILL.md'
    assert 'oa-dealer-closure' in collected['rule_file']
    result = app.submit(collected['task_id'], opinion='✅ 无余额，不退款。❗ 冰柜待核。', confirm_write=True)
    assert result['status'] == 'submitted'
    assert result['submit_ok'] and result['done_hit'] and result['postcheck_done_hit']
    assert api.posts == [('101', '✅ 无余额，不退款。❗ 冰柜待核。', 'comment')]
    assert len(app.inspect(collected['task_id'])['submissions']) == 1


@pytest.mark.parametrize('closure_app', ['ai_empty_bi_empty'], indirect=True)
def test_empty_bi_preserves_unknowns_and_continues_ai_preaudit(closure_app):
    app, api, collected = closure_app
    material = json.loads(Path(collected['material_file']).read_text())
    evidence = material['dealer_closure_evidence']
    assert evidence['bi']['rows'] == []
    assert evidence['sap'] == {'company_code': 'D001', 'customer_code': '99000001'}
    assert len(evidence['dms']) == 4
    assert evidence['branch_context']['deferred_fields'] == ['yetk', 'sftk']
    opinion = '❗ BI无客户记录，余额、预提、冰柜、融资及补贴待核；余额分支及退款意向由后续节点确认。'
    result = app.submit(collected['task_id'], opinion=opinion, confirm_write=True)
    assert result['status'] == 'submitted'
    assert result['submit_ok'] and result['done_hit'] and result['postcheck_done_hit']
    assert api.posts == [('101', opinion, 'approval')]


def test_changed_feedback_button_stops_before_submission_attempt(closure_app):
    app, api, collected = closure_app
    api.form['submitButtonName'] = '提交'
    with pytest.raises(RuntimeError, match='dealer_closure_feedback_context_invalid'):
        app.submit(collected['task_id'], opinion='✅ 合成意见。', confirm_write=True)
    assert api.posts == []
    assert app.inspect(collected['task_id'])['submissions'] == []


def test_changed_customer_or_amount_stops_before_submission_attempt(closure_app):
    app, api, collected = closure_app
    api.form['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields'][1]['fieldValue'] = '99000002'
    with pytest.raises(ValueError, match='dealer_closure_form_changed'):
        app.submit(collected['task_id'], opinion='✅ 合成意见。', confirm_write=True)
    assert api.posts == []
    assert app.inspect(collected['task_id'])['submissions'] == []


@pytest.mark.parametrize('change', ['nonempty', 'missing'])
def test_incomplete_closing_attachment_is_not_hidden_by_generic_zero_count(closure_app, change):
    from apps.oa.approvals.collection.case import collect_case

    app, api, _ = closure_app
    fields = api.form['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields']
    fields.append({'fieldName': 'attachment_count', 'fieldShowName': '附件数量', 'fieldValue': '0'})
    next(f for f in fields if f['fieldName'] == 'fj')['fieldValue'] = ''
    if change == 'nonempty':
        next(f for f in fields if f['fieldName'] == 'fjsc1')['fieldValue'] = 'unresolved_statement_reference'
    else:
        fields[:] = [f for f in fields if f['fieldName'] != 'tksqs']
    material = collect_case('101', oa_api=api, configs=flows.load_all_configs(), source_policy=POLICY,
        allow_browser_download=False, attachment_root=app.root / 'extra-attachments', prepared_root=app.root / 'extra-prepared')
    assert 'attachment_presence' not in material


@pytest.mark.parametrize('closure_app', ['ai_empty'], indirect=True)
def test_ai_preaudit_freezes_unknown_branches_and_previews_without_writing(closure_app, monkeypatch):
    app, api, collected = closure_app
    material = json.loads(Path(collected['material_file']).read_text())
    evidence = material['dealer_closure_evidence']
    assert evidence['branch_context']['deferred_fields'] == ['yetk', 'sftk']
    assert evidence['form_snapshot']['fields']['yetk'] == ''
    assert evidence['form_snapshot']['fields']['sftk'] == ''
    assert evidence['bi']['rows'][0]['cust_balance'] == '0.00'
    assert len(evidence['dms']) == 4
    result = app.submit(collected['task_id'], opinion='❗ 余额及退款意向尚未填写，后续节点确认。', confirm_write=False)
    assert result['status'] == 'preview'
    assert api.posts == []
    fields = {f['fieldName']: f['fieldValue'] for f in api.form['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields']}
    assert fields['yetk'] == fields['sftk'] == ''
    original_query = dealer_closure.official_sql._query_rows
    def changed_fields(name, *a, **kw):
        rows = original_query(name, *a, **kw)
        return [{**r, 'ismandatory': '1'} for r in rows] if name == 'dealer_closure_branch_fields' else rows
    monkeypatch.setattr(dealer_closure.official_sql, '_query_rows', changed_fields)
    with pytest.raises(RuntimeError, match='dealer_closure_branch_invalid'):
        dealer_closure.validate_submission(api, material)
    assert api.posts == []
