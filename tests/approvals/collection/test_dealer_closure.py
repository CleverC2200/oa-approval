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
        self.form['workflowBaseInfo'] = {'workflowId': '195127', 'workflowName': '经销商管理-关户'}
        self.posts = []

    def pending_rows(self):
        return deepcopy(self.rows)

    def request_context(self, request_id):
        return deepcopy(self.form), []

    def submit_and_verify(self, request_id, opinion, *, dealer_material):
        self.posts.append((request_id, opinion, dealer_material['dealer_closure_evidence']['submission_context']['delivery_kind']))
        self.rows = []
        return {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}


@pytest.fixture
def closure_app(tmp_path, monkeypatch):
    actual = {'requestid': 101, 'currentnodeid': 30, 'currentnodetype': '3', 'nodename': '归档'}
    monkeypatch.setattr(dealer_closure.official_sql, '_query_rows',
        lambda name, *_a, **_kw: [{'operator_id': 9}] if name == 'dealer_closure_forward_log' else [actual.copy()])
    monkeypatch.setattr(dealer_closure, 'settings', lambda: {})
    def worker(mode, payload, _config):
        if mode == 'doris':
            return {'rows': [{'company_code': 'D001', 'cust_code': '99000001', 'cust_balance': '0.00',
                              'freezers_qty': None, 'ledger_account_control': None, 'pre_end_balance': '0.00'}]}
        return {'company_code': payload['company_code'], 'customer_code': payload['customer_code']}
    monkeypatch.setattr(dealer_closure, '_worker', worker)
    monkeypatch.setattr(dealer_closure, '_dms', lambda *_: {'response': {'success': True, 'result': None}})
    monkeypatch.setattr(invoices, 'read_invoice_buyers', lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError('expense source called')))
    api = ClosureApi()
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
    assert Path(collected['rule_file']).name == 'SKILL.md'
    assert 'oa-dealer-closure' in collected['rule_file']
    result = app.submit(collected['task_id'], opinion='✅ 无余额，不退款。❗ 冰柜待核。', confirm_write=True)
    assert result['status'] == 'submitted'
    assert result['submit_ok'] and result['done_hit'] and result['postcheck_done_hit']
    assert api.posts == [('101', '✅ 无余额，不退款。❗ 冰柜待核。', 'comment')]
    assert len(app.inspect(collected['task_id'])['submissions']) == 1


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
