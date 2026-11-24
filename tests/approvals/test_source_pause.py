from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from apps.oa.approvals import store
from apps.oa.approvals.client import query
from apps.oa.approvals.collection import case
from apps.oa.approvals.operations import Approval
from apps.oa.approvals.sources import invoice_titles
from apps.oa.approvals.sources.travel_evidence import TravelEvidence
from tests.approvals.collection.test_case import FLOW_CONFIGS, RecordedOaApi
from tests.approvals.collection.test_invoice_coverage import cloud_http
from tests.approvals.invoice_fakes import InvoiceApi, linked_field
from tests.approvals.invoice_title_fakes import InvoiceTitleTransport, Response
from tests.approvals.source_fakes import POLICY


@pytest.fixture
def source_pause(tmp_path, monkeypatch):
    configs = {config['flow_key']: config for config in FLOW_CONFIGS}
    rows = []
    collected = []
    clock = [datetime(2026, 12, 1, tzinfo=timezone.utc)]
    requests = []
    failed_reads = [2]

    class OA:
        def pending_rows(self):
            return rows

        def request_context(self, request_id):
            collected.append(request_id)
            key = next(row['workflow_key'] for row in rows if row['request_id'] == request_id)
            config = configs[key]
            return RecordedOaApi(config['workflow_id'], config['workflow_name']).request_context(request_id)

    monkeypatch.setattr(store, 'now', lambda: clock[0].isoformat())
    monkeypatch.setattr(case, '_travel_resources', lambda **_kwargs: TravelEvidence.empty())
    transport = InvoiceTitleTransport()

    def read(request, *, timeout):
        requests.append(request)
        return (Response(b'<html>no session</html>') if len(requests) <= failed_reads[0]
                else transport(request, timeout=timeout))

    monkeypatch.setattr(invoice_titles, 'urlopen', read)
    app = Approval(tmp_path, api=OA(), configs=FLOW_CONFIGS, source_policy=POLICY)
    yield app, rows, collected, clock, requests, failed_reads
    app.store.connection.close()


def pending_rows(rows):
    rows.extend({'request_id': str(i), 'received_at': f'2026-12-01 08:00:0{i}',
                 'workflow_key': workflow, 'operator_id': 'tester'}
                for i, workflow in enumerate(('general_expense', 'general_expense',
                                              'packaging_layout_approval', 'packaging_layout_approval'), 1))


def test_hourly_source_pause_keeps_batch_scope_and_resumes_original_fifo(source_pause):
    app, rows, collected, clock, requests, _failed_reads = source_pause
    pending_rows(rows)
    run = app.pending(batch_size=3, hourly=True)['run_id']

    paused = app.collect(run, expected_task_id=1)
    assert paused['status'] == 'waiting' and paused['reason'] == 'source_unavailable'
    diagnostic = Path(paused['source_pause']['diagnostics_file'])
    diagnostics = json.loads(diagnostic.read_text())['diagnostics']
    assert [(row['reason'], row['source_check']) for row in diagnostics] == [
        ('session_missing', 1), ('session_missing', 2),
    ]
    assert diagnostic.is_relative_to(app.root / 'collection-errors')
    snapshot = query(app.root, 'status', view='batch')
    assert snapshot['next_candidate']['id'] == 3
    assert snapshot['source_pause']['waiting_task_ids'] == [1, 2]
    assert snapshot['isolated_failures']['items'] == []
    assert app.collect(run, expected_task_id=2)['reason'] == 'expected_task_changed'
    assert app.collect(run, expected_task_id=3)['status'] == 'prepared'
    app.fail(run, 3, 'synthetic_unrelated_failure')
    assert app.collect(run, expected_task_id=4)['reason'] == 'source_unavailable'
    assert collected == ['1', '3'] and len(requests) == 2

    clock[0] += timedelta(minutes=2)
    assert query(app.root, 'status', view='batch')['next_candidate']['id'] == 1
    first = app.collect(run, expected_task_id=1)
    assert first['status'] == 'prepared'
    assert query(app.root, 'status', view='batch')['source_pause']['recovered_at']
    assert app.store.get_task(1)['failure_count'] == 0 and diagnostic.is_file()
    app.fail(run, 1, 'synthetic_first_review_failure')
    second = app.collect(run, expected_task_id=2)
    assert second['status'] == 'prepared'
    assert first['material_file'] != second['material_file']
    assert all(json.loads(Path(result['material_file']).read_text())['invoice_title_reference']['status'] == 'ready'
               for result in (first, second))
    assert sum(request.data is None for request in requests) == 4
    assert collected == ['1', '3', '1', '2']
    assert app.store.get_task(4)['status'] == 'queued'


def test_all_source_waiting_can_close_and_continue_without_burning_a_candidate(source_pause):
    app, rows, collected, _clock, requests, _failed_reads = source_pause
    pending_rows(rows)
    run = app.pending(batch_size=2, hourly=True)['run_id']
    assert app.collect(run, expected_task_id=1)['status'] == 'waiting'
    assert app.collect(run, expected_task_id=2)['reason'] == 'source_unavailable'
    closed = app.close(run, handoff=True)
    assert closed['handoff']['status'] == 'ready'
    assert closed['execution']['stop']['kind'] == 'source_unavailable'
    assert closed['execution']['failure_count'] == 0
    status = query(app.root, 'status', view='batch')
    assert status['hourly_dispatch']['action'] == 'continue'
    continued = app.pending(continue_from=run, hourly=True)['run_id']
    assert app.collect(continued, expected_task_id=1)['status'] == 'prepared'
    assert app.inspect(1)['submissions'] == []
    assert collected == ['1', '1'] and sum(request.data is None for request in requests) == 3


def test_single_bad_response_does_not_pause_hourly_collection(source_pause):
    app, rows, collected, _clock, requests, failed_reads = source_pause
    failed_reads[0] = 1
    pending_rows(rows)
    run = app.pending(batch_size=2, hourly=True)['run_id']
    prepared = app.collect(run, expected_task_id=1)
    assert prepared['status'] == 'prepared'
    reference = json.loads(Path(prepared['material_file']).read_text())['invoice_title_reference']
    assert reference['diagnostics'][0]['source_check'] == 1
    report = query(app.root, 'status', view='batch')
    assert 'source_pause' not in report and report['next_candidate']['id'] == 2
    assert collected == ['1'] and sum(request.data is None for request in requests) == 2


def test_hourly_source_recovery_with_missing_invoice_original_keeps_case_isolated(source_pause, cloud_http, monkeypatch):
    app, rows, collected, clock, requests, _failed_reads = source_pause
    pending_rows(rows)
    recorded_context = app.api.request_context

    def request_context(request_id):
        workflow, resources = recorded_context(request_id)
        if request_id == '1':
            detail = workflow['workflowDetailTableInfos'][0]['workflowRequestTableRecords'][0]
            detail['workflowRequestTableFields'].append(linked_field())
        return workflow, resources

    monkeypatch.setattr(app.api, 'request_context', request_context)
    monkeypatch.setattr(app.api, 'get', InvoiceApi().get, raising=False)
    run = app.pending(batch_size=2, hourly=True)['run_id']
    assert app.collect(run, expected_task_id=1)['reason'] == 'source_unavailable'

    clock[0] += timedelta(minutes=2)
    with pytest.raises(case.CollectionFailure, match='invoice_original_coverage_incomplete') as failure:
        app.collect(run, expected_task_id=1)
    snapshot = query(app.root, 'status', run_id=run, view='batch')
    assert snapshot['source_pause']['recovered_at'] == clock[0].isoformat()
    assert snapshot['next_candidate']['id'] == 2
    isolated, = snapshot['isolated_failures']['items']
    assert isolated['task_id'] == 1 and isolated['reason'] == 'invoice_original_coverage_incomplete'
    assert isolated['evidence_file'] == failure.value.diagnostics_file
    failed = query(app.root, 'show', task_id=1)['task']
    material = json.loads(Path(failed['material_file']).read_text())
    assert material['invoice_title_reference']['status'] == 'ready'
    assert material['invoice_evidence']['invoices'][0]['body_read_status'] == 'not_read'
    assert app.collect(run, expected_task_id=2)['status'] == 'prepared'
    assert collected == ['1', '1', '2'] and sum(request.data is None for request in requests) == 4


def test_unreserved_source_waiting_close_requires_hourly_recovery_evidence(source_pause):
    app, rows, _collected, _clock, _requests, _failed_reads = source_pause
    pending_rows(rows)
    run = app.pending(batch_size=2, hourly=True)['run_id']
    assert app.collect(run, expected_task_id=1)['status'] == 'waiting'
    app.close(run)
    status = query(app.root, 'status', view='batch')
    assert status['handoff'] is None
    assert status['hourly_dispatch']['action'] == 'confirm_stopped'
