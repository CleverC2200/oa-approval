import sys
from types import SimpleNamespace

import pytest

from apps.oa.approvals.sources import dealer_closure_reader as reader


class SapRecording:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.identity = {'RFCSYSID': 'P01', 'RFCIPADDR': '192.0.2.1', 'RFCHOST2': 'synthetic-production'}
        self.tables = {'T000': [{'MANDT': '600', 'MTEXT': 'Production Client', 'CCCATEGORY': 'P', 'LOGSYS': 'SYNTHETIC'}],
                       'T001': [{'BUKRS': 'D001', 'BUTXT': '合成公司', 'WAERS': 'CNY'}],
                       'KNKK': [{'KUNNR': '0099000001', 'KKBER': 'S001', 'KNKLI': '0099000001',
                                 'KLIMK': '0.00', 'SKFOR': '12500.00-', 'SSOBL': '0.00', 'CRBLB': ''}]}

    def call(self, function, **params):
        self.calls.append((function, params))
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


def test_reader_preserves_raw_sign_window_and_closes_readonly_connection(monkeypatch):
    connection = SapRecording()
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    result = reader.sap(payload())
    assert result['status'] == 'ok'
    assert result['queries']['credit']['rows'][0]['SKFOR'] == '12500.00-'
    assert result['financing_window_start'] == '20250930'
    assert connection.closed
    assert {f for f, _ in connection.calls} == {'RFC_SYSTEM_INFO', 'RFC_READ_TABLE', 'RPY_PROGRAM_READ'}
    assert all(p['ROWCOUNT'] == 10000 for f, p in connection.calls if f == 'RFC_READ_TABLE')


def test_company_ledger_is_scoped_and_active_contract_amounts_include_following_orders(monkeypatch):
    connection = SapRecording()
    connection.tables.update({
        'VAKPA': [{'KUNDE': '0099000001', 'VKORG': 'D001', 'PARVW': 'RG', 'VBELN': '0000001001'}],
        'VBUK': [{'VBELN': '0000001001', 'RFGSK': 'A'}],
        'VBAK': [{'VBELN': '0000001001', 'VKORG': 'D001', 'VBTYP': 'G', 'WAERK': 'CNY', 'AUDAT': '20260301'}],
        'VBAP': [{'VBELN': '0000001001', 'POSNR': '000010', 'KZWI2': '100.00', 'ABGRU': ''}],
        'VBUP': [{'VBELN': '0000001001', 'POSNR': '000010', 'RFSTA': 'A'}],
        'VBFA': [{'VBELV': '0000001001', 'POSNV': '000010', 'VBELN': '0000002001', 'POSNN': '000010', 'VBTYP_N': 'C'}],
    })
    original = connection.call
    def call(function, **params):
        if function == 'RFC_READ_TABLE' and params['QUERY_TABLE'] in {'KNB1', 'BSID', 'BSAD'}:
            assert "BUKRS = 'D001'" in ' '.join(r['TEXT'] for r in params['OPTIONS'])
        if function == 'RFC_READ_TABLE' and params['QUERY_TABLE'] == 'VBAP' and "VBELN = '0000002001'" in params['OPTIONS'][0]['TEXT']:
            connection.calls.append((function, params))
            return {'DATA': [{'WA': '0000002001|000010|40.00'}]}
        return original(function, **params)
    connection.call = call
    monkeypatch.setitem(sys.modules, 'pyrfc', SimpleNamespace(Connection=lambda **_: connection))
    result = reader.sap(payload())
    amounts = result['contract_amounts']['0000001001']
    assert amounts['items']['rows'][0]['KZWI2'] == '100.00'
    assert amounts['following_orders'][0]['amounts'][0]['rows'][0]['KZWI2'] == '40.00'
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
