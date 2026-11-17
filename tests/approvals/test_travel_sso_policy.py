from tests.approvals.review_fixtures import deliver_and_submit
import copy
import io
import json
import os
from pathlib import Path

import pytest
import openpyxl

from apps.oa.approvals import cli, owner
from apps.oa.approvals.operations import Approval
from tests.approvals.collection.test_empty_attachments import TRAVEL_CONFIG, travel_source
from tests.approvals.source_fakes import POLICY
from tests.approvals.test_operations import FakeOA


@pytest.fixture
def sso_case(tmp_path, monkeypatch, travel_source):
    workflow, scan, _ = travel_source
    scan.update(text='', diagnostics=[{'stage': 'session', 'reason': 'travel_sso_account_missing'}])
    api = FakeOA()
    api.rows = [{**api.rows[0], 'request_id': str(number)} for number in (12, 13)]

    def context(request_id):
        value = copy.deepcopy(workflow)
        value['requestId'] = request_id
        return value, []

    api.request_context = context
    root = tmp_path / 'run'
    app = Approval(root, api=api, writer=api, configs=[TRAVEL_CONFIG], source_policy=POLICY)
    policy = Path(os.environ['OA_AUTH_ENV_FILE']).with_name('travel-sso-authorization.json')
    authorization = {'authorization_note': 'Synthetic authorization to hand off only missing SSO account or company',
                     'allowed_reasons': ['travel_sso_account_missing', 'travel_sso_company_missing']}
    run = app.pending(batch_size=2)['run_id']
    monkeypatch.setattr(cli, 'send', lambda _root, request: owner.dispatch(app, run, request))
    yield app, api, scan, policy, authorization, run
    app.store.connection.close()


def collect_cli(app, task_id, capsys):
    result = cli.main(['--root', str(app.root), 'collect', '--expected-task-id', str(task_id)])
    return result, json.loads(capsys.readouterr().out)


def submit_cli(app, collected, capsys, *, status='business_pending', recognition_file=None):
    task = app.store.get_task(collected['task_id'])
    directory = Path(collected['material_file']).parent
    review = directory / 'review.json'
    review.write_text(json.dumps({'task_id': task['id'], 'material_sha256': task['material_sha256'],
        'status': status, 'reason': 'Known OA materials reviewed; travel page and undiscovered files remain unread',
        'rule_basis': 'Synthetic explicit SSO handoff authorization',
        'evidence_files': [task['material_file'], *([recognition_file] if recognition_file else [])]}))
    opinion = directory / 'opinion.txt'
    opinion.write_text('SSO account unavailable; travel details remain unverified. Hand off for manual review.')
    from tests.approvals.review_fixtures import deliver_cli

    def invoke(*arguments):
        code = cli.main(['--root', str(app.root), *arguments])
        return code, json.loads(capsys.readouterr().out)
    return deliver_cli(app.root, invoke, task['id'], opinion, review_file=review,
                       recognition_file=recognition_file, confirm_write=True)



@pytest.mark.parametrize('reason', ['travel_sso_account_missing', 'travel_sso_company_missing'])
def test_default_collect_binds_authorized_sso_handoff_then_submits_and_continues_fifo(sso_case, capsys, reason):
    app, api, scan, policy, authorization, run = sso_case
    scan['diagnostics'][0]['reason'] = reason
    policy.write_text(json.dumps(authorization))
    code, collected = collect_cli(app, 1, capsys)
    assert code == 0, collected
    material = json.loads(Path(collected['material_file']).read_text())
    exception = material['travel_read_exception']
    assert exception['request_id'] == '12'
    assert exception['diagnostics'][0]['reason'] == reason
    assert exception['authorization_note'] == authorization['authorization_note']
    assert 'attachment_presence' not in material
    assert api.posts == []
    code, receipt = submit_cli(app, collected, capsys)
    assert code == 0, receipt
    assert receipt['submit_ok'] and receipt['done_hit'] and receipt['postcheck_done_hit']
    assert cli.main(['--root', str(app.root), 'postcheck', '--task-id', '1']) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked['status'] == 'submitted'
    assert json.loads(checked['submissions'][0]['result_json'])['postcheck_done_hit'] is True
    assert len(api.posts) == 1
    code, second = collect_cli(app, 2, capsys)
    assert code == 0, second
    second_material = json.loads(Path(second['material_file']).read_text())
    assert second_material['travel_read_exception']['request_id'] == '13'
    assert app.store.get_task(1)['status'] == 'submitted'
    assert len(api.posts) == 1


@pytest.mark.parametrize('diagnostics', [
    [{'stage': 'session', 'reason': 'login_required'}],
    [{'stage': 'session', 'reason': 'travel_sso_rejected'}],
    [{'stage': 'browser', 'reason': 'navigation_failed'}],
    [{'stage': 'session', 'reason': 'travel_sso_account_missing'},
     {'stage': 'download', 'reason': 'resource_ownership_untrusted'}],
])
def test_sso_policy_never_waives_other_or_mixed_failures(sso_case, capsys, diagnostics):
    app, api, scan, policy, authorization, _ = sso_case
    policy.write_text(json.dumps(authorization))
    scan['diagnostics'] = diagnostics
    code, result = collect_cli(app, 1, capsys)
    assert code == 1 and result['status'] == 'failed'
    assert app.store.get_task(1)['status'] == 'failed'
    assert api.posts == []
    assert app.inspect(1)['submissions'] == []


@pytest.mark.parametrize('authorization', [
    {}, {'authorization_note': 'Synthetic', 'allowed_reasons': ['login_required']},
    {'authorization_note': '', 'allowed_reasons': ['travel_sso_account_missing']},
])
def test_invalid_sso_authorization_stops_before_claim(sso_case, capsys, authorization):
    app, api, _, policy, _, _ = sso_case
    policy.write_text(json.dumps(authorization))
    code, result = collect_cli(app, 1, capsys)
    assert code == 1 and result['error'] == 'travel_sso_authorization_invalid'
    assert app.store.get_task(1)['status'] == 'queued'
    assert api.posts == []


def test_sso_handoff_cannot_be_submitted_as_fully_verified(sso_case, capsys):
    app, api, _, policy, authorization, _ = sso_case
    policy.write_text(json.dumps(authorization))
    code, collected = collect_cli(app, 1, capsys)
    assert code == 0, collected
    code, result = submit_cli(app, collected, capsys, status='complete')
    assert code == 1 and result['error'] == 'travel_exception_requires_business_pending'
    assert api.posts == [] and app.inspect(1)['review_assessments'] == []
    assert submit_cli(app, collected, capsys)[0] == 0


def test_exact_failed_delivery_is_recollected_without_erasing_failure_or_resending(sso_case, capsys, monkeypatch):
    app, api, _, policy, authorization, run = sso_case
    code, failed = collect_cli(app, 1, capsys)
    assert code == 1 and failed['error'] == 'travel_attachment_discovery_failed'
    diagnostics = Path(failed['diagnostics_file'])
    original = diagnostics.read_bytes()
    policy.write_text(json.dumps(authorization))
    code, next_delivery = collect_cli(app, 2, capsys)
    assert code == 0, next_delivery
    assert submit_cli(app, next_delivery, capsys)[0] == 0
    assert app.store.get_task(1)['status'] == 'failed'
    assert app.inspect(1)['submissions'] == []
    app.close(run)
    ordinary = app.pending(request_ids=['12'])
    assert ordinary['tasks'] == []
    app.close(ordinary['run_id'])
    policy.write_text(json.dumps(authorization))
    retry = app.pending(request_ids=['12'], retry_failed=True, batch_size=1)['run_id']
    monkeypatch.setattr(cli, 'send', lambda _root, request: owner.dispatch(app, retry, request))
    code, collected = collect_cli(app, 1, capsys)
    assert code == 0, collected
    assert app.store.get_task(1)['failure_count'] == 1
    assert diagnostics.read_bytes() == original
    assert submit_cli(app, collected, capsys)[0] == 0
    assert submit_cli(app, collected, capsys)[0] == 0
    assert [request_id for request_id, _ in api.posts].count('12') == 1
    assert len(api.posts) == 2
    assert len(app.inspect(1)['submissions']) == 1
    assert diagnostics.read_bytes() == original


def test_unknown_sso_handoff_stops_fifo_and_never_resends(sso_case, capsys):
    app, api, _, policy, authorization, _ = sso_case
    policy.write_text(json.dumps(authorization))

    def unknown(request_id, opinion):
        api.posts.append((request_id, opinion))
        return {'submit_ok': True, 'done_hit': False, 'postcheck_done_hit': False}

    api.submit_and_verify = unknown
    code, collected = collect_cli(app, 1, capsys)
    assert code == 0, collected
    _, result = submit_cli(app, collected, capsys)
    assert result['status'] == 'unknown'
    code, candidate = collect_cli(app, 2, capsys)
    assert candidate['status'] == 'blocked'
    assert app.store.get_task(2)['status'] == 'queued'
    _, duplicate = submit_cli(app, collected, capsys)
    assert duplicate['status'] == 'unknown'
    assert len(api.posts) == 1


@pytest.mark.parametrize('download_fails', [False, True])
def test_sso_handoff_reads_known_attachments_and_keeps_each_resource_before_submission(
        sso_case, monkeypatch, capsys, download_fails):
    from apps.oa.approvals.collection import downloads
    from apps.oa.approvals.sources.download_errors import Downloaded
    from apps.oa.materials import cli as materials_cli, resources
    from tests.approvals.source_fakes import download_sources

    app, api, _, policy, authorization, _ = sso_case
    policy.write_text(json.dumps(authorization))
    original_context = api.request_context
    known = [{'id': str(number), 'name': 'synthetic-expenses.xlsx',
              'downloadUrl': f'/download?fileid={number}'} for number in (201, 202)]
    api.request_context = lambda request_id: (original_context(request_id)[0], known)
    workbook = openpyxl.Workbook()
    workbook.active.append(['Expense', 'Amount'])
    workbook.active.append(['Synthetic known meal', 131.25])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    original = output.getvalue()
    attempted = []

    def download(resource, *, dest_dir, **_options):
        attempted.append(resource['id'])
        if download_fails:
            raise RuntimeError('synthetic_known_attachment_unreadable')
        path = dest_dir / resources.deterministic_filename(resource)
        path.write_bytes(original)
        return Downloaded(path)

    sources = download_sources(download)
    sources.transfer.rpa_storage_state = lambda **_options: {'cookies': [], 'origins': []}
    monkeypatch.setattr(downloads, 'build_download_sources', lambda _policy: sources)
    code, collected = collect_cli(app, 1, capsys)
    assert set(attempted) == {'201', '202'}
    if download_fails:
        assert code == 1 and collected['status'] == 'failed'
        assert app.store.get_task(1)['status'] == 'failed'
        with pytest.raises(ValueError, match='task_not_reviewable'):
            deliver_and_submit(app, 1, opinion='Cannot submit unread materials', confirm_write=True)
        assert api.posts == []
        return
    assert code == 0, collected
    material = json.loads(Path(collected['material_file']).read_text())
    assert {r['resource_id'] for r in material['resources']} == {'201', '202'}
    assert all(Path(r['local_readonly_path']).read_bytes() == original for r in material['resources'])
    offset, pieces, digest = 0, [], None
    while offset is not None:
        args = ['read', collected['material_file'], '--format', 'json', '--pointer', '/prepared/structured_contents',
                '--page-offset', str(offset)]
        if digest:
            args += ['--expected-sha256', digest]
        assert materials_cli.main(args) == 0
        page = json.loads(capsys.readouterr().out)
        assert page['offset'] == offset
        digest = page['material_sha256']
        pieces.append(page['text'])
        offset = page['next_offset']
    assert 'Synthetic known meal' in ''.join(pieces) and '131.25' in ''.join(pieces)
    assert materials_cli.main(['recognize', '--material-file', collected['material_file']]) == 0
    recognition = json.loads(capsys.readouterr().out)
    assert recognition['status'] == 'complete'
    report = json.loads(Path(recognition['report_file']).read_text())
    assert {p['resource_id'] for p in report['parts']} == {'201', '202'}
    assert all(p['source'] == 'parser' for p in report['parts'])
    assert submit_cli(app, collected, capsys, recognition_file=recognition['report_file'])[0] == 0
    assert len(api.posts) == 1
    assert {a['resource_id'] for a in app.inspect(1)['attachments']} == {'201', '202'}
