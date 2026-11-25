import re
import sys
from types import SimpleNamespace

import pytest

from apps.oa.approvals.sources import dealer_closure_reader as reader


class SapRecording:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.credit_response = {'RETURN': [{'TYPE': 'S', 'ID': '0099000001'}],
                                'O_DKLIMK': '638.62-', 'O_OKLIMK': '999.00',
                                'O_ORDER': '3.15', 'O_DELIVERY': '2.45',
                                'O_INVOICE': '0.20-', 'O_CONTRACT': '4.00'}
        self.identity = {'RFCSYSID': 'P01', 'RFCIPADDR': '192.0.2.1', 'RFCHOST2': 'synthetic-production'}
        self.tables = {'T000': [{'MANDT': '600', 'MTEXT': 'Production Client', 'CCCATEGORY': 'P', 'LOGSYS': 'SYNTHETIC'}],
                       'T001': [{'BUKRS': 'D001', 'BUTXT': '合成公司', 'WAERS': 'CNY'}],
                       'KNKK': [{'KUNNR': '0099000001', 'KKBER': 'S001', 'KNKLI': '0099000001',
                                 'KLIMK': '0.00', 'SKFOR': '12500.00-', 'SSOBL': '0.00', 'CRBLB': ''}]}

    def call(self, function, **params):
        self.calls.append((function, params))
        if function == 'ZSD_CRM_CREDITLIMIT_EXPORT':
            return self.credit_response
        assert function in {'RFC_SYSTEM_INFO', 'RFC_READ_TABLE', 'RPY_PROGRAM_READ'}
        if function == 'RFC_SYSTEM_INFO':
            return {'RFCSI_EXPORT': self.identity}
        if function == 'RPY_PROGRAM_READ':
            return {'SOURCE': ['SYNTHETIC READ-ONLY PROGRAM']}
        assert params['GET_SORTED'] == 'X'
        fields = [r['FIELDNAME'] for r in params['FIELDS']]
        return {'DATA': [{'WA': '|'.join(str(row.get(f, '')) for f in fields)}
                         for row in self.tables.get(params['QUERY_TABLE'], [])[params.get('ROWSKIPS', 0):params.get('ROWSKIPS', 0) + params['ROWCOUNT']]]}

    def close(self):
        self.closed = True


def payload():
    return {'company_code': 'D001', 'customer_code': '99000001', 'as_of': '20260331',
            'connection': {'sap_host': '192.0.2.1', 'sap_system': 'P01', 'sap_sysnr': '01',
                           'sap_client': '600', 'sap_user': 'synthetic-reader'}, 'sap_password': 'synthetic-secret'}


def test_reader_uses_business_credit_results_and_preserves_company_ledger_scope(monkeypatch):
    connection = SapRecording()
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    result = reader.sap(payload())
    assert result['status'] == 'ok'
    assert result['queries']['credit']['rows'][0]['SKFOR'] == '12500.00-'
    assert result['financing_window_start'] == '20250930'
    assert connection.closed
    assert result['credit_limit'] == {
        'source': 'ZSD_CRM_CREDITLIMIT_EXPORT', 'customer_code': '0099000001', 'credit_control_area': 'SN01',
        'remaining_order_limit': '-638.62', 'order_occupancy': '9.40', 'response': connection.credit_response,
    }
    assert [params for function, params in connection.calls if function == 'ZSD_CRM_CREDITLIMIT_EXPORT'] == [
        {'KUNNR': '0099000001'},
    ]
    ledger_tables = {'KNB1', 'BSID', 'BSAD'}
    previous_credit_tables = {'S066', 'S067', 'VAKPA', 'VBUK', 'VBAK', 'VBAP', 'VBUP', 'VBFA'}
    reads = [params for function, params in connection.calls if function == 'RFC_READ_TABLE']
    assert {params['QUERY_TABLE'] for params in reads}.isdisjoint(previous_credit_tables)
    assert {params['QUERY_TABLE'] for params in reads if params['QUERY_TABLE'] in ledger_tables} == ledger_tables
    assert all("BUKRS = 'D001'" in ' '.join(row['TEXT'] for row in params['OPTIONS'])
               for params in reads if params['QUERY_TABLE'] in ledger_tables)
    assert all(params['ROWCOUNT'] == 10000 for params in reads)
    assert set(result['programs']) == {'ZFRR04', 'ZFRR08'}


def test_preaccrual_batches_cover_document_keys_and_keep_customer_details(monkeypatch):
    connection = SapRecording()
    account = '0000123456'
    connection.tables['KNVP'] = [{'KUNNR': '0099000001', 'VKORG': 'D001', 'PARVW': 'RG', 'KUNN2': '0099000002'}]
    connection.credit_response['RETURN'] = [{'TYPE': 'S', 'ID': '0099000002'}]
    keys = [('2025', f'{document:010d}') for document in range(1, 402)] + [('2026', '0000000001')]
    expected = {keys[index] for index in (0, 399, 400, 401)}
    connection.tables['BSIS'] = [
        {'BUKRS': 'D001', 'HKONT': account, 'GJAHR': year, 'BELNR': document, 'BUZEI': '001',
         'DMBTR': '12.50', 'SHKZG': 'H'} for year, document in keys
    ]
    lines = {(row['GJAHR'], row['BELNR']): {
        **row, 'XREF1': '99000001' if (row['GJAHR'], row['BELNR']) in expected else '99000002',
    } for row in connection.tables['BSIS']}
    reference_batches = []
    original = connection.call

    def call(function, **params):
        if function == 'RFC_READ_TABLE' and params['QUERY_TABLE'] == 'BSEG':
            options = ' '.join(row['TEXT'] for row in params['OPTIONS'])
            assert "BUKRS = 'D001'" in options
            year = re.search(r"GJAHR = '([0-9]{4})'", options)[1]
            if 'BELNR IN (' in options:
                assert f"HKONT = '{account}'" in options
                documents = re.findall(r"'([0-9]{10})'", options.partition('BELNR IN (')[2])
                reference_batches.append((year, len(documents)))
            else:
                assert "BUZEI = '001'" in options
                documents = [re.search(r"BELNR = '([0-9]{10})'", options)[1]]
            connection.tables['BSEG'] = [lines[(year, document)] for document in documents]
        return original(function, **params)

    connection.call = call
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    evidence, = reader.sap({**payload(), 'preaccrual_accounts': [account]})['preaccrual']

    assert evidence['complete'] and evidence['index_rows'] == evidence['index_keys_seen'] == 402
    assert {(row['GJAHR'], row['BELNR']) for row in evidence['target_rows']} == expected
    details = [row for result in evidence['target_details'] for row in result['rows']]
    assert {(row['GJAHR'], row['BELNR'], row['DMBTR']) for row in details} == {
        (year, document, '12.50') for year, document in expected
    }
    assert reference_batches == [('2025', 400), ('2025', 1), ('2026', 1)]


@pytest.mark.parametrize('merged_credit', [False, True])
def test_business_credit_accepts_sap_payer_scope_and_filtered_group_messages(monkeypatch, merged_credit):
    connection = SapRecording()
    connection.tables['KNVP'] = [{'KUNNR': '0099000001', 'VKORG': 'D001', 'PARVW': 'RG', 'KUNN2': '0099000002'}]
    connection.tables['T014'] = [{'KKBER': 'SN01', 'WAERS': 'CNY'}]
    if merged_credit:
        connection.tables['ZTCREDITLIMIT01'] = [
            {'ZJXS': '001', 'ZJXSBM': '0099000001'}, {'ZJXS': '001', 'ZJXSBM': '0099000003'},
        ]
        connection.credit_response['RETURN'] = []
    else:
        connection.credit_response['RETURN'] = [{'TYPE': 'S', 'ID': '0099000002'}]

    original = connection.call

    def call(function, **params):
        if function == 'RFC_READ_TABLE' and params['QUERY_TABLE'] == 'T014':
            if "KKBER = 'SN01'" not in params['OPTIONS'][0]['TEXT']:
                return {'DATA': []}
        return original(function, **params)

    connection.call = call
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    result = reader.sap({**payload(), 'preaccrual_accounts': ['0000123456']})
    assert result['credit_limit']['remaining_order_limit'] == '-638.62'
    assert result['credit_limit']['response']['RETURN'] == connection.credit_response['RETURN']
    assert result['credit_currencies']['SN01']['rows'] == [{'KKBER': 'SN01', 'WAERS': 'CNY'}]
    if merged_credit:
        assert result['queries']['group_members']['rows'] == connection.tables['ZTCREDITLIMIT01']


@pytest.mark.parametrize(('customer', 'response_id', 'extra_payer'), [
    ('0049000001', None, None),
    ('0099000001', '0099000002', None),
    ('0099000001', None, '0099000003'),
])
def test_business_credit_rejects_group_messages_outside_sap_payer_rules(
        monkeypatch, customer, response_id, extra_payer):
    connection = SapRecording()
    payer = {'KUNNR': customer, 'VKORG': 'D001', 'PARVW': 'RG', 'KUNN2': '0099000002'}
    connection.tables['KNVP'] = [payer]
    if extra_payer:
        connection.tables['KNVP'].append({**payer, 'VKORG': 'D002', 'KUNN2': extra_payer})
    connection.tables['ZTCREDITLIMIT01'] = [{'ZJXS': '001', 'ZJXSBM': customer}]
    connection.credit_response['RETURN'] = [{'TYPE': 'S', 'ID': response_id}] if response_id else []
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    with pytest.raises(RuntimeError, match='dealer_closure_credit_identity_unverified'):
        reader.sap({**payload(), 'customer_code': customer})
    assert connection.closed


@pytest.mark.parametrize(('changes', 'reason'), [
    ({'RETURN': [{'TYPE': 'S', 'ID': '0099000001'}, {'TYPE': 'E', 'MESSAGE': 'private message'}]},
     'dealer_closure_credit_business_error'),
    ({'RETURN': [{'TYPE': 'S', 'ID': '0099000002'}]}, 'dealer_closure_credit_identity_unverified'),
    ({'RETURN': [{'TYPE': 'S'}]}, 'dealer_closure_credit_identity_unverified'),
    ({'RETURN': []}, 'dealer_closure_credit_identity_unverified'),
    ({'O_DKLIMK': None}, 'dealer_closure_credit_response_invalid'),
    ({'O_CONTRACT': 'NaN'}, 'dealer_closure_credit_response_invalid'),
])
def test_reader_rejects_failed_or_unbound_business_credit_result(monkeypatch, changes, reason):
    connection = SapRecording()
    connection.credit_response.update(changes)
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    with pytest.raises(RuntimeError, match=reason) as captured:
        reader.sap(payload())
    assert reader.failure(captured.value) == {
        'status': 'failed', 'error_type': 'RuntimeError', 'reason': reason, 'stage': 'credit_limit', 'error_key': '',
    }
    assert sum(function == 'ZSD_CRM_CREDITLIMIT_EXPORT' for function, _ in connection.calls) == 1
    assert connection.closed


@pytest.mark.parametrize('failure', ['environment', 'client', 'cardinality'])
def test_reader_rejects_wrong_environment_and_nonproduction_client(monkeypatch, failure):
    connection = SapRecording()
    if failure == 'environment':
        connection.identity['RFCSYSID'] = 'Q01'
    elif failure == 'client':
        connection.tables['T000'][0]['CCCATEGORY'] = 'T'
    else:
        connection.tables['T000'] *= 10000
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    with pytest.raises(RuntimeError, match={'environment': 'environment_mismatch',
                                          'client': 'client_not_production', 'cardinality': 'client_not_production'}[failure]):
        reader.sap(payload())
    assert connection.closed
    assert not any(function == 'ZSD_CRM_CREDITLIMIT_EXPORT' for function, _ in connection.calls)
    if failure == 'cardinality':
        try:
            reader.sap(payload())
        except RuntimeError as error:
            assert reader.failure(error)['reason'] == 'dealer_closure_sap_client_not_production'
            assert reader.failure(error)['stage'] == 'source'


def test_worker_failure_never_echoes_arbitrary_error_text():
    assert reader.failure(RuntimeError('synthetic-secret'))['reason'] == 'dealer_closure_source_failed'


@pytest.mark.parametrize('count', [10000, 10001])
def test_reader_paginates_and_verifies_all_rows(monkeypatch, count):
    connection = SapRecording()
    connection.tables['KNB1'] = [{'KUNNR': str(index), 'BUKRS': 'D001'} for index in range(count)]
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    result = reader.sap(payload())['queries']['company_accounts']
    assert len(result['rows']) == count
    assert result['rows'][-1]['KUNNR'] == str(count - 1)
    assert [page['request']['ROWSKIPS'] for page in result['pages']] == [0, 10000]
    assert result['verification_pages'] == result['pages']
    assert connection.closed


@pytest.mark.parametrize('failure', ['changed', 'stalled', 'limit'])
def test_reader_rejects_inconsistent_or_unbounded_pages(monkeypatch, failure):
    connection = SapRecording()
    connection.tables['KNB1'] = [{'KUNNR': str(index)} for index in range(10001)]
    original = connection.call
    first_reads = 0

    def call(function, **params):
        nonlocal first_reads
        if function == 'RFC_READ_TABLE' and params['QUERY_TABLE'] == 'KNB1':
            if params['ROWSKIPS'] == 0:
                first_reads += 1
            if failure == 'stalled':
                params['ROWSKIPS'] = 0
            if failure == 'changed' and first_reads > 1:
                connection.tables['KNB1'][0]['KUNNR'] = 'changed'
        return original(function, **params)

    connection.call = call
    if failure == 'limit':
        monkeypatch.setattr(reader, '_MAX_SAP_ROWS', 10000)
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    with pytest.raises(RuntimeError, match='snapshot_changed' if failure == 'changed' else 'rows_truncated') as captured:
        reader.sap(payload())
    assert captured.value.source_stage == 'KNB1'
    assert connection.closed
