from tests.approvals.review_fixtures import collect_for_review
from datetime import datetime
import json
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from apps.oa.approvals.collection.case import CollectionFailure, collect_case
from apps.oa.approvals.operations import Approval
from apps.oa.approvals.sources import invoice_titles
from tests.approvals.collection.test_case import FLOW_CONFIGS, RecordedOaApi
from tests.approvals.invoice_title_fakes import InvoiceTitleTransport, Response, ROWS
from tests.approvals.source_fakes import POLICY


def test_native_report_keeps_literal_names_and_only_required_reference_fields(monkeypatch):
    transport = InvoiceTitleTransport()
    monkeypatch.setattr(invoice_titles, 'urlopen', transport)
    reference = invoice_titles.snapshot()
    assert reference['status'] == 'ready'
    assert reference['rows'] == ROWS
    assert reference['source_url'] == invoice_titles.SOURCE_URL
    assert datetime.fromisoformat(reference['observed_at']) <= datetime.fromisoformat(reference['completed_at'])
    assert len(transport.requests) == 5
    assert all(response.closed for response in transport.responses)
    assert 'private' not in json.dumps(reference)


@pytest.mark.parametrize('fault', ['login', 'redirect', 'oversize', 'timeout', 'bad_json', 'empty_list',
                                 'pagination', 'missing_tax_id', 'wrong_company', 'duplicate_company',
                                 'duplicate_tax_id', 'duplicate_field'])
def test_incomplete_or_changed_reference_fails_without_leaking_response(monkeypatch, fault):
    transport = InvoiceTitleTransport()
    responses = []

    def read(request, *, timeout):
        if fault == 'timeout':
            raise TimeoutError('private-session-and-business-data')
        response = transport(request, timeout=timeout)
        raw = response.getvalue()
        response.close()
        if fault == 'login':
            raw = b'<html>private-login</html>'
        elif fault == 'oversize':
            raw = b'x' * (invoice_titles.MAX_RESPONSE_BYTES + 1)
        elif request.data:
            if fault == 'bad_json':
                raw = b'private-invalid-response'
            else:
                data = json.loads(raw)
                if fault == 'pagination':
                    data['totalPage'] = 2
                elif fault == 'empty_list' and b'REPORT1' in request.data:
                    data['htmlTag'] = '<table></table>'
                elif fault == 'missing_tax_id':
                    data['htmlTag'] = data['htmlTag'].replace('税号:', 'private-other-field:')
                elif fault == 'wrong_company' and b'REPORT0' in request.data:
                    data['htmlTag'] = data['htmlTag'].replace(ROWS[0]['company_name'], '错误公司')
                elif fault == 'duplicate_company' and b'REPORT1' in request.data:
                    data['htmlTag'] += f'<td>{ROWS[0]["company_name"]}</td>'
                elif fault == 'duplicate_tax_id':
                    data['htmlTag'] = data['htmlTag'].replace(ROWS[1]['tax_id'], ROWS[0]['tax_id'])
                elif fault == 'duplicate_field' and b'REPORT0' in request.data:
                    data['htmlTag'] += '<td>税号:</td><td>private-duplicate</td>'
                raw = json.dumps(data).encode()
        result = Response(raw)
        if fault == 'redirect':
            result.getcode = lambda: 302
        responses.append(result)
        return result

    monkeypatch.setattr(invoice_titles, 'urlopen', read)
    with pytest.raises(invoice_titles.InvoiceTitleReadError) as error:
        invoice_titles.snapshot()
    assert str(error.value) == 'invoice_title_reference_read_failed'
    if fault == 'empty_list':
        assert error.value.diagnostics[0]['reason'] == 'company_list_empty'
    assert all(response.closed for response in responses)


@pytest.mark.parametrize('failure', [TimeoutError('private-token'),
                                    URLError(TimeoutError('private-token')),
                                    HTTPError('https://private.test/?token=secret', 503, 'private-body', {}, None)])
@pytest.mark.parametrize('stage', ['bootstrap', 'company_details'])
def test_transient_reference_reads_retry_only_failed_request(monkeypatch, failure, stage):
    transport = InvoiceTitleTransport()
    attempts = []
    delays = []

    def read(request, **options):
        selected = request.data is None if stage == 'bootstrap' else request.data and b'REPORT0' in request.data
        if selected and len(attempts) < 2:
            attempts.append(request)
            raise failure
        return transport(request, **options)

    monkeypatch.setattr(invoice_titles, 'urlopen', read)
    monkeypatch.setattr(invoice_titles, 'sleep', delays.append)
    reference = invoice_titles.snapshot()
    assert reference['rows'] == ROWS
    assert len(transport.requests) == 5
    assert delays == [0.25, 0.5]
    assert [item['attempt'] for item in reference['diagnostics']] == [1, 2]
    assert all(item['stage'] == stage for item in reference['diagnostics'])
    assert 'private' not in json.dumps(reference) and 'secret' not in json.dumps(reference)


def test_reference_retry_exhaustion_keeps_safe_details_without_partial_rows(monkeypatch):
    transport = InvoiceTitleTransport()
    attempts = []
    monkeypatch.setattr(invoice_titles, 'sleep', lambda _delay: None)

    def read(request, **options):
        if request.data and b'REPORT0' in request.data:
            attempts.append(request)
            raise HTTPError('https://private.test/?session=secret', 503, 'private-body', {}, None)
        return transport(request, **options)

    monkeypatch.setattr(invoice_titles, 'urlopen', read)
    with pytest.raises(invoice_titles.InvoiceTitleReadError) as captured:
        invoice_titles.snapshot()
    assert len(attempts) == 3
    assert [item['attempt'] for item in captured.value.diagnostics] == [1, 2, 3]
    assert all(item['stage'] == 'company_details' and item['http_status'] == 503
               and item['source_path'] == 'invoice_title_reference.rows[0]'
               for item in captured.value.diagnostics)
    assert 'private' not in json.dumps(captured.value.diagnostics)
    assert 'secret' not in json.dumps(captured.value.diagnostics)


def test_reference_closes_http_error_responses_before_retry(monkeypatch):
    transport = InvoiceTitleTransport()
    responses = []

    def read(request, **options):
        if not responses:
            response = Response(b'private-error-body')
            responses.append(response)
            raise HTTPError('https://private.test', 503, 'private', {}, response)
        assert responses[0].closed
        return transport(request, **options)

    monkeypatch.setattr(invoice_titles, 'urlopen', read)
    monkeypatch.setattr(invoice_titles, 'sleep', lambda _delay: None)
    assert invoice_titles.snapshot()['rows'] == ROWS
    assert all(response.closed for response in responses + transport.responses)


def test_reference_retry_keeps_original_deadline(monkeypatch):
    clock = [0.0]
    transport = InvoiceTitleTransport()
    attempts = []
    monkeypatch.setattr(invoice_titles, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(invoice_titles, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))

    def read(request, **options):
        attempts.append(options['timeout'])
        clock[0] += 20
        if len(attempts) == 3:
            raise TimeoutError('private')
        return transport(request, **options)

    monkeypatch.setattr(invoice_titles, 'urlopen', read)
    with pytest.raises(invoice_titles.InvoiceTitleReadError) as captured:
        invoice_titles.snapshot()
    assert len(attempts) == 3
    assert captured.value.diagnostics[-1]['reason'] == 'deadline'


@pytest.mark.parametrize('failure', [HTTPError('https://private.test/?secret=x', 403, 'private', {}, None),
                                    URLError(ssl.SSLCertVerificationError('private-certificate'))])
def test_reference_does_not_retry_permission_or_tls_failures(monkeypatch, failure):
    attempts = []

    def read(*_args, **_options):
        attempts.append(True)
        raise failure

    monkeypatch.setattr(invoice_titles, 'urlopen', read)
    monkeypatch.setattr(invoice_titles, 'sleep', lambda _delay: pytest.fail('must not retry'))
    with pytest.raises(invoice_titles.InvoiceTitleReadError) as captured:
        invoice_titles.snapshot()
    assert len(attempts) == 1
    assert len(captured.value.diagnostics) == 1
    assert 'private' not in json.dumps(captured.value.diagnostics)


def test_reference_is_frozen_without_an_applicable_company_row_and_cannot_change(tmp_path):
    company = '没有登记的合成公司'

    class FieldApi(RecordedOaApi):
        def request_context(self, request_id):
            workflow, resources = super().request_context(request_id)
            workflow['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields'].append({
                'fieldName': 'bxgs', 'fieldShowName': '报销公司', 'fieldShowValue': company,
            })
            return workflow, resources

    config = next(config for config in FLOW_CONFIGS if config['flow_key'] == 'general_expense')
    api = FieldApi(config['workflow_id'], config['workflow_name'])
    api.pending_rows = lambda: [{'request_id': 'synthetic', 'workflow_key': 'general_expense',
                                'received_at': '2026-01-01 09:00:00', 'operator_id': 'tester'}]
    app = Approval(tmp_path, api=api, configs=[config], source_policy=POLICY)
    task = app.collect(app.pending()['run_id'], expected_task_id=1)
    assert task['status'] == 'prepared'
    material_file = Path(task['material_file'])
    material = json.loads(material_file.read_text())
    assert material['invoice_title_reference']['rows'] == ROWS
    assert any(field['name'] == '报销公司' and field['value'] == company for field in material['declared_fields'])
    assert all(row['company_name'] != company for row in material['invoice_title_reference']['rows'])
    assert app.inspect(task['task_id'])['submissions'] == []
    app._frozen(app.store.get_task(task['task_id']))
    material['invoice_title_reference']['rows'][0]['company_name'] = '已篡改名称'
    material_file.write_text(json.dumps(material))
    with pytest.raises(ValueError, match='frozen_material_changed'):
        app._frozen(app.store.get_task(task['task_id']))


@pytest.mark.parametrize(('bootstrap', 'stage', 'reason', 'shape', 'count'), [
    (b'<html>private-login</html>', 'bootstrap', 'session_missing', 'markup', 0),
    (b'{"widgetUrl":"?sessionID=private_a","other":{"widgetUrl":"?sessionID=private_b"}}',
     'bootstrap', 'session_ambiguous', 'json_like', 2),
    (None, 'bootstrap', 'network_error', None, None),
    (None, 'company_list', 'company_list_empty', 'report', None),
])
def test_reference_read_failure_stops_complete_collection(tmp_path, monkeypatch, bootstrap, stage, reason, shape, count):
    empty_report = b'{"htmlTag":"<table></table>","totalPage":1,"currentPage":1}'
    transport = InvoiceTitleTransport()

    def read(request, *, timeout):
        if reason == 'network_error':
            raise TimeoutError('private-timeout')
        if stage == 'company_list':
            return Response(empty_report) if request.data else transport(request, timeout=timeout)
        return Response(bootstrap)

    config = next(config for config in FLOW_CONFIGS if config['flow_key'] == 'general_expense')
    monkeypatch.setattr(invoice_titles, 'urlopen', read)
    monkeypatch.setattr(invoice_titles, 'sleep', lambda _seconds: None)
    api = RecordedOaApi(config['workflow_id'], config['workflow_name'])
    api.pending_rows = lambda: [{'request_id': 'synthetic', 'workflow_key': 'general_expense',
                                'received_at': '2026-01-01 09:00:00', 'operator_id': 'tester'}]
    app = Approval(tmp_path, api=api, configs=[config], source_policy=POLICY)
    with pytest.raises(CollectionFailure, match='invoice_title_reference_read_failed') as captured:
        collect_for_review(app, app.pending()['run_id'], agent_id='reviewer', expected_task_id=1,
                    agent_workflow_key='general_expense')
    report = json.loads(Path(captured.value.diagnostics_file).read_text())
    assert report['diagnostics'] == captured.value.diagnostics
    first = report['diagnostics'][0]
    assert first['stage'] == stage and first['reason'] == reason
    assert first.get('http_status') == (200 if shape else None)
    assert first.get('response_shape') == shape
    assert first.get('response_bytes_read') == (len(bootstrap or empty_report) if shape else None)
    assert first.get('widget_url_count') == count
    assert first.get('session_count') == count
    assert first['attempt'] == 1 and first['elapsed_ms'] >= 0
    assert 'private' not in json.dumps({key: value for key, value in report.items() if key != 'material_directory'})
    assert app.inspect(1)['submissions'] == []
