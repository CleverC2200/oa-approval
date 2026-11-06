from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.oa.approvals.sources import dealer_closure as source
from apps.oa.approvals.sources.oa import _pending_case, OaApi


def workflow():
    return {'requestId': '101', 'canEdit': True, 'currentNodeId': '20', 'submitButtonName': '批注',
            'workflowRequestLogs': [{'nodeId': '20', 'operateType': '转发', 'operatorId': '9',
                                     'operateDate': '2026-10-30', 'operateTime': '10:00:00'}],
            'workflowMainTableInfo': {'requestRecords': [{'workflowRequestTableFields': [
                {'fieldName': name, 'fieldValue': value} for name, value in
                [('gsdm', 'D001'), ('jxsdm', '99000001'), ('yetk', '1'), ('sftk', '0')]]}]}}


def delivery(**changes):
    return {'request_id': '101', 'task_instance_id': '1001', 'operator_id': '7',
            'operator_node_id': '20',
            'delivery_kind': 'comment', 'view_type': '0', 'received_at': '2026-10-30 10:00:00', **changes}


@pytest.fixture
def context(monkeypatch):
    node = {'requestid': 101, 'currentnodeid': 30, 'currentnodetype': '3', 'nodename': '归档'}
    monkeypatch.setattr(source.official_sql, '_query_rows',
        lambda name, *_a, **_kw: ([{'operator_id': 9}] if name == 'dealer_closure_forward_log'
            else [] if name == 'dealer_closure_history' else [node.copy()]))
    return node


def test_pending_keeps_unread_forward_delivery_but_not_read_comment_or_other_flow(monkeypatch):
    def row(request, flow, view, remark, cid):
        return {'requestId': request, 'workflowBaseInfo': {'workflowId': flow, 'workflowName': '测试'},
                'userid': '7', 'receiveTime': '2026-10-30 10:00:00', 'viewtype': view,
                'isremark': remark, 'cid': cid, 'nodeid': '20'}
    rows = [row('101', '195127', '0', '1', '1001'), row('102', '195127', '-2', '1', '1002'),
            row('103', '195127', '-2', '0', '1003'), row('104', '195021', '-2', '0', '1004')]
    api = OaApi('synthetic-token', 'synthetic-user', app_id='synthetic-app', base_url='https://oa.example.test')
    monkeypatch.setattr(api, '_post_readonly', lambda *_a: {'code': 'SUCCESS', 'data': rows})
    result = api.pending_rows()
    assert {r['request_id'] for r in result} == {'101', '103', '104'}
    assert _pending_case(rows[0])['task_instance_id'] == '1001'
    assert next(r for r in result if r['request_id'] == '103')['delivery_kind'] == 'approval'


def test_no_balance_collection_skips_refund_and_keeps_raw_null_scoped_confirmation(monkeypatch, context):
    api = SimpleNamespace(pending_rows=lambda: [delivery()])
    confirmation = {'request_id': '101', 'task_instance_id': '1001', 'company_code': 'D001',
                    'customer_code': '99000001', 'confirmed_at': '2026-11-04', 'note': '合成范围确认无冰柜'}
    monkeypatch.setattr(source, 'settings', lambda: {'freezer_none_confirmations': [confirmation]})
    def worker(mode, payload, _config):
        if mode == 'doris':
            return {'rows': [{'company_code': 'D001', 'cust_code': '99000001', 'freezers_qty': None,
                              'ledger_account_control': None, 'pre_end_balance': '0.00'}]}
        return {'company_code': payload['company_code'], 'customer_code': payload['customer_code']}
    monkeypatch.setattr(source, '_worker', worker)
    dms = Mock(return_value={'response': {'success': True, 'result': None}})
    monkeypatch.setattr(source, '_dms', dms)
    result = source.read(api, workflow(), [])
    assert set(result['dms']) == {'querySignedOneAddNContractByCustomerCode'}
    assert dms.call_args[0][1] == 'querySignedOneAddNContractByCustomerCode'
    assert result['bi']['rows'][0]['freezers_qty'] is None
    assert result['freezer_none_confirmation'] == confirmation
    assert result['submission_context']['forwarder_id'] == '9'
    assert result['submission_context']['actual_node'] == context
    assert result['history'] == {'source': 'official_sql.dealer_closure_history', 'complete': True, 'rows': []}
    changed = delivery(task_instance_id='1002')
    api.pending_rows = lambda: [changed]
    assert source.read(api, workflow(), [])['freezer_none_confirmation'] is None


def test_wrong_customer_bi_is_rejected_before_other_sources(monkeypatch, context):
    api = SimpleNamespace(pending_rows=lambda: [delivery()])
    monkeypatch.setattr(source, 'settings', lambda: {})
    worker = Mock(return_value={'rows': [{'company_code': 'D001', 'cust_code': '99000002'}]})
    monkeypatch.setattr(source, '_worker', worker)
    with pytest.raises(RuntimeError, match='dealer_closure_bi_identity_invalid'):
        source.read(api, workflow(), [])
    assert worker.call_count == 1


def test_context_change_or_archived_normal_approval_cannot_write(monkeypatch, context):
    api = SimpleNamespace(pending_rows=lambda: [delivery()])
    expected = source.snapshot_context(api, workflow())
    api.pending_rows = lambda: [delivery(task_instance_id='1002')]
    api.request_context = lambda _: (workflow(), [])
    with pytest.raises(ValueError, match='dealer_closure_submission_context_changed'):
        source.validate_submission(api, {'request_id': '101', 'dealer_closure_evidence': {
            'submission_context': expected, 'form_snapshot': source._form_snapshot(workflow(), [])}})
    api.pending_rows = lambda: [delivery(delivery_kind='approval')]
    current = workflow()
    current['submitButtonName'] = '提交'
    with pytest.raises(RuntimeError, match='dealer_closure_actual_node_invalid'):
        source.snapshot_context(api, current)


def test_feedback_receipt_requires_new_exact_comment_and_unchanged_original_node(monkeypatch, context):
    api = SimpleNamespace(pending_rows=lambda: [delivery()])
    expected = source.snapshot_context(api, workflow())
    material = {'request_id': '101', 'dealer_closure_evidence': {'submission_context': expected}}
    entry = {'remark': '✅ 金额0元<br/>❗ 权限未锁'}
    entry['remark_length'] = len(entry['remark'])
    def query(name, params, **_):
        if name == 'dealer_closure_current_node':
            return [context.copy()]
        assert params == {'request_id': 101, 'node_id': 20, 'operator_id': 7,
                          'started_date': '2026-11-05', 'started_time': '10:00:00'}
        return [entry.copy()]
    monkeypatch.setattr(source.official_sql, '_query_rows', query)
    started = datetime(2026, 11, 5, 2, 0, 0, tzinfo=timezone.utc).isoformat()
    assert source.feedback_verified(api, material, '✅ 金额0元\n❗ 权限未锁', started)
    assert not source.feedback_verified(api, material, '✅ 金额1元\n❗ 权限未锁', started)
    entry['remark_length'] += 1
    assert not source.feedback_verified(api, material, '✅ 金额0元\n❗ 权限未锁', started)
    entry['remark_length'] -= 1
    context['currentnodeid'] = 31
    assert not source.feedback_verified(api, material, '✅ 金额0元\n❗ 权限未锁', started)


def test_private_settings_and_worker_failure_do_not_expose_credentials(tmp_path, monkeypatch):
    config = tmp_path / 'dealer-closure.json'
    config.write_text(json.dumps({}))
    config.chmod(0o644)
    monkeypatch.setattr(source, 'stable_env_file', lambda: tmp_path / 'auth.env')
    with pytest.raises(RuntimeError, match='configuration_unavailable'):
        source.settings()
    monkeypatch.setattr(source.official_sql, '_keychain_password', lambda *_: 'synthetic-secret')
    monkeypatch.setattr(source.subprocess, 'run', lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout='synthetic-secret'))
    with pytest.raises(RuntimeError) as captured:
        source._worker('sap', {}, {'sap_python': 'synthetic-python', 'sap_sdk': '/synthetic/sdk',
                                  'sap_user': 'synthetic-user', 'sap_keychain_service': 'synthetic-service'})
    assert str(captured.value) == 'dealer_closure_sap_read_failed'


def test_dms_null_is_data_but_cross_customer_is_rejected(monkeypatch):
    body = {'success': True, 'result': None}
    class Response:
        status = 200
        def __enter__(self):
            from io import StringIO
            result = StringIO(json.dumps(body))
            result.status = self.status
            return result
        def __exit__(self, *_):
            pass
    monkeypatch.setattr(source.urllib.request, 'build_opener', lambda *_: SimpleNamespace(open=lambda *_a, **_kw: Response()))
    config = {'dms_origin': 'https://dms.example.test'}
    assert source._dms('99000001', 'queryLatestRefundApplyByCustomerCode', config)['response']['result'] is None
    body['result'] = {'customerCode': '99000002'}
    with pytest.raises(RuntimeError, match='dealer_closure_dms_read_failed'):
        source._dms('99000001', 'queryLatestRefundApplyByCustomerCode', config)


def test_worker_retains_safe_source_failure_diagnostics(monkeypatch):
    from apps.oa.approvals.collection.case import CollectionFailure

    monkeypatch.setattr(source.official_sql, '_keychain_password', lambda *_: 'synthetic-secret')
    monkeypatch.setattr(source.subprocess, 'run', lambda *_a, **_kw: SimpleNamespace(returncode=1,
        stdout=json.dumps({'status': 'failed', 'reason': 'dealer_closure_sap_rows_truncated',
                           'stage': 'BSID', 'error_type': 'RuntimeError', 'error_key': 'unsafe secret text'})))
    with pytest.raises(CollectionFailure) as captured:
        source._worker('sap', {}, {'sap_python': 'synthetic-python', 'sap_sdk': '/synthetic/sdk',
                                  'sap_user': 'synthetic-user', 'sap_keychain_service': 'synthetic-service'})
    assert captured.value.diagnostics == [{'stage': 'sap_BSID',
        'reason': 'dealer_closure_sap_rows_truncated', 'exception_type': 'RuntimeError'}]


@pytest.mark.parametrize('rows', [[{'remark': '历史意见', 'remark_length': 9}], [{}] * 1001])
def test_incomplete_history_stops_before_financial_sources(monkeypatch, context, rows):
    api = SimpleNamespace(pending_rows=lambda: [delivery()])
    original = source.official_sql._query_rows
    monkeypatch.setattr(source.official_sql, '_query_rows',
        lambda name, *a, **kw: rows if name == 'dealer_closure_history' else original(name, *a, **kw))
    worker = Mock(side_effect=AssertionError('financial source called'))
    monkeypatch.setattr(source, '_worker', worker)
    with pytest.raises(RuntimeError, match='dealer_closure_history_incomplete'):
        source.read(api, workflow(), [])
    worker.assert_not_called()


def test_missing_branch_is_diagnosed_without_external_account_reads(monkeypatch, context):
    from apps.oa.approvals.collection.case import CollectionFailure
    data = workflow()
    for field in data['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields']:
        if field['fieldName'] in {'yetk', 'sftk'}:
            field['fieldValue'] = ''
    monkeypatch.setattr(source, '_worker', lambda *a: pytest.fail('must not read accounts'))
    with pytest.raises(CollectionFailure, match='dealer_closure_branch_invalid') as captured:
        source.read(SimpleNamespace(pending_rows=lambda: [delivery()]), data, [])
    assert captured.value.diagnostics == [
        {'stage': 'dealer_closure_branch', 'source_path': code, 'reason': 'missing_value'}
        for code in ('yetk', 'sftk')]
