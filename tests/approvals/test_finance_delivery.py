import json

import pytest

from apps.oa.approvals import finance_precheck
from apps.oa.approvals.sources import prediction, transfer
from tests.approvals.test_operations import service


@pytest.fixture
def delivery(monkeypatch):
    value = {'action': 'transfer', 'recipient_id': '8', 'source_node_id': '20'}
    monkeypatch.setattr(finance_precheck, 'load_policy', lambda: {'leader': {'id': '100'}})
    def predict(request_id, *, refresh):
        assert request_id == '101' and refresh is True
        return {'finance_precheck': {'status': 'required'}, 'delivery': dict(value)}
    monkeypatch.setattr(prediction, 'predict', predict)
    return value


def task(app, workflow_key='travel_expense'):
    app.api.rows[0]['workflow_key'] = workflow_key
    collector = app.collector
    def collect(*args, **kwargs):
        material = collector(*args, **kwargs)
        return {**material, 'workflow_key': workflow_key}
    app.collector = collect
    return app.collect(app.pending()['run_id'], agent_id='synthetic-reviewer',
        expected_task_id=1, agent_workflow_key=workflow_key)['task_id']


@pytest.mark.parametrize('workflow_key', ['travel_expense', 'business_entertainment', 'business_support_fee',
                                        'general_expense', 'small_marketing_fee'])
def test_matching_case_previews_then_transfers_with_persisted_target_and_never_submits(tmp_path, monkeypatch, delivery, workflow_key):
    app, api = service(tmp_path)
    task_id = task(app, workflow_key)
    calls = []
    def prepare(request_id, plan):
        calls.append('prepare')
        return {**plan, 'operator_id': '7'}
    def send(request_id, opinion, context):
        recorded = json.loads(app.inspect(task_id)['submissions'][-1]['result_json'])
        assert recorded['delivery'] == context
        assert app.inspect(task_id)['task']['status'] == 'submitting'
        assert opinion == '合成审核意见'
        calls.append('transfer')
        return {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}
    monkeypatch.setattr(api, 'prepare_transfer', prepare, raising=False)
    monkeypatch.setattr(api, 'transfer_and_verify', send, raising=False)
    preview = app.submit(task_id, opinion='合成审核意见')
    assert preview['delivery']['action'] == 'transfer'
    assert calls == []
    result = app.submit(task_id, opinion='合成审核意见', confirm_write=True)
    assert result['status'] == 'submitted'
    assert result['delivery']['recipient_id'] == '8'
    assert calls == ['prepare', 'transfer']
    assert api.posts == []
    assert app.submit(task_id, opinion='禁止重复', confirm_write=True)['status'] == 'submitted'
    assert calls == ['prepare', 'transfer']
    from apps.oa.approvals.client import query
    from apps.oa.approvals.owner import _RoundSession

    run_id = app.store.get_task(task_id)['last_run_id']
    session = _RoundSession(app, run_id, root=tmp_path, demo=True, close_transport=lambda: None)
    responses = []
    session.handle(lambda: {'operation': 'finish', 'run_id': run_id, 'require_drained': True},
                   source='socket', respond=responses.append)
    assert responses[0]['status'] == 'closed'
    assert query(tmp_path, 'status', view='batch')['closeout']['complete'] is True
    assert calls == ['prepare', 'transfer']


def test_blocked_or_changed_delivery_never_starts_a_write(tmp_path, monkeypatch, delivery):
    app, api = service(tmp_path)
    task_id = task(app)
    delivery.update(action='blocked', reason='finance_recipient_ambiguous')
    assert app.submit(task_id, opinion='合成审核意见')['delivery']['action'] == 'blocked'
    with pytest.raises(ValueError, match='finance_recipient_ambiguous'):
        app.submit(task_id, opinion='合成审核意见', confirm_write=True)
    delivery['action'] = 'transfer'
    def prepare(*args):
        raise ValueError('transfer_target_changed')
    monkeypatch.setattr(api, 'prepare_transfer', prepare, raising=False)
    with pytest.raises(ValueError, match='transfer_target_changed'):
        app.submit(task_id, opinion='合成审核意见', confirm_write=True)
    assert app.inspect(task_id)['submissions'] == []
    assert api.posts == []


def test_unknown_transfer_uses_saved_transfer_evidence_for_readonly_recovery(tmp_path, monkeypatch, delivery):
    app, api = service(tmp_path)
    task_id = task(app)
    monkeypatch.setattr(api, 'prepare_transfer', lambda req, plan: {**plan, 'operator_id': '7'}, raising=False)
    def send(*args):
        raise TimeoutError()
    monkeypatch.setattr(api, 'transfer_and_verify', send, raising=False)
    app.submit(task_id, opinion='合成审核意见')
    app.submit(task_id, opinion='另一份未发送意见')
    result = app.submit(task_id, opinion='合成审核意见', confirm_write=True)
    assert result['status'] == 'unknown'
    assert app.submit(task_id, opinion='禁止重发', confirm_write=True)['status'] == 'unknown'
    calls = []
    def verify(client, request_id, opinion, started_at, context):
        assert context['operator_id'] == '7' and context['recipient_id'] == '8'
        assert opinion == '合成审核意见'
        calls.append('readonly')
        return {'delivery': context, 'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}
    monkeypatch.setattr(transfer, 'verify', verify)
    recovered = app.postcheck(task_id)
    assert recovered['status'] == 'submitted'
    assert calls == ['readonly']
    assert api.posts == []


def test_negative_match_retains_normal_submit(tmp_path, delivery):
    app, api = service(tmp_path)
    task_id = task(app)
    delivery['action'] = 'submit'
    assert app.submit(task_id, opinion='合成审核意见', confirm_write=True)['status'] == 'submitted'
    assert api.posts == [('101', '合成审核意见')]


def test_tpm_keeps_original_preview_and_submit_without_finance_precheck(tmp_path, monkeypatch, delivery):
    app, api = service(tmp_path)
    task_id = task(app, 'tpm_small_marketing_fee')
    def unexpected(*args, **kwargs):
        pytest.fail('excluded workflow must not load finance policy, predict, or transfer')
    monkeypatch.setattr(finance_precheck, 'load_policy', unexpected)
    monkeypatch.setattr(prediction, 'predict', unexpected)
    monkeypatch.setattr(api, 'prepare_transfer', unexpected, raising=False)
    monkeypatch.setattr(api, 'transfer_and_verify', unexpected, raising=False)
    preview = app.submit(task_id, opinion='合成审核意见')
    assert preview['status'] == 'preview' and 'delivery' not in preview
    assert api.posts == []
    result = app.submit(task_id, opinion='合成审核意见', confirm_write=True)
    assert result['status'] == 'submitted' and 'delivery' not in result
    assert api.posts == [('101', '合成审核意见')]
