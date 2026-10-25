from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
import pytest

from apps.oa.materials import recognize as recognition


def test_progress_exposes_early_report_and_saved_partial_failure(tmp_path, monkeypatch):
    from apps.oa.materials.progress import progress

    files = []
    for number, color in enumerate(('red', 'blue'), 1):
        image = tmp_path / f'{number}.png'
        Image.new('RGB', (3, 2), color).save(image)
        files.append(image)
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-secret-key')
    events = []
    snapshots = []

    def on_progress(event):
        events.append(event)
        assert progress(Path(event['report_file'])) == event

    def post(body, *, key, timeout):
        report_file = Path(events[0]['report_file'])
        before = report_file.read_bytes()
        snapshots.append(progress(report_file))
        assert report_file.read_bytes() == before
        return {'choices': [{'finish_reason': 'stop' if len(snapshots) == 1 else 'length',
                             'message': {'content': 'private recognized text'}}]}

    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(files=files, output_dir=tmp_path / 'recognized', on_progress=on_progress)

    assert events[0]['status'] == 'running'
    assert events[0]['completed_parts'] == 0
    assert events[0]['planned_parts'] is None
    assert [(item['completed_parts'], item['planned_parts']) for item in snapshots] == [(0, 2), (1, 2)]
    assert events[-1] == progress(Path(report['report_file']))
    assert events[-1]['status'] == 'failed'
    assert events[-1]['coverage'] == 'all_prepared_content'
    assert events[-1]['completed_parts'] == 1
    assert events[-1]['planned_parts'] == 2
    assert events[-1]['error_code'] == 'attachment_response_incomplete'
    assert events[-1]['failed_part']['part_id'] == 'image-2'
    assert 'private recognized text' not in json.dumps(events)
    assert 'fake-secret-key' not in json.dumps(events)
    assert report['parts'][0]['answer'] == 'private recognized text'


def test_progress_rejects_absent_and_invalid_reports_without_writes(tmp_path):
    from apps.oa.materials.progress import progress

    absent = tmp_path / 'absent' / 'recognition.json'
    with pytest.raises(ValueError, match='^attachment_recognition_report_missing$'):
        progress(absent)
    assert not absent.parent.exists()
    report_file = tmp_path / 'recognition.json'
    for raw in ('{', 'null', '[]', '{}', json.dumps({
        'schema_version': 1, 'kind': 'deepseek_attachment_recognition', 'status': 'complete',
        'completed_at': '2026-10-25T00:00:00+00:00', 'parts': [], 'read_plan': ['image-1']})):
        report_file.write_text(raw)
        before = report_file.stat()
        with pytest.raises(ValueError, match='^attachment_recognition_report_invalid$'):
            progress(report_file)
        assert report_file.read_text() == raw
        assert report_file.stat().st_mtime_ns == before.st_mtime_ns
    assert list(tmp_path.iterdir()) == [report_file]


def test_recognize_cli_progress_is_opt_in_and_keeps_final_stdout(tmp_path, capsys):
    from apps.oa.materials import cli

    source = tmp_path / 'source.txt'
    source.write_text('private complete source text')
    arguments = ['recognize', '--file', str(source)]
    assert cli.main([*arguments, '--output-dir', str(tmp_path / 'default')]) == 0
    default = capsys.readouterr()
    assert default.err == ''
    assert json.loads(default.out)['status'] == 'complete'
    assert cli.main([*arguments, '--output-dir', str(tmp_path / 'events'), '--progress']) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    events = [json.loads(line) for line in output.err.splitlines()]
    assert events[0]['status'] == 'running'
    assert events[0]['planned_parts'] is None
    assert events[-1]['status'] == 'complete'
    assert (events[-1]['completed_parts'], events[-1]['planned_parts']) == (1, 1)
    assert {event['report_file'] for event in events} == {result['report_file']}
    assert 'private complete source text' not in output.out + output.err
    assert cli.main(['progress', result['report_file']]) == 0
    queried = capsys.readouterr()
    assert queried.err == ''
    assert json.loads(queried.out) == events[-1]
    absent = tmp_path / 'absent' / 'recognition.json'
    assert cli.main(['progress', str(absent)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        'status': 'failed', 'error_code': 'attachment_recognition_report_missing'}
    assert not absent.parent.exists()


def test_progress_keeps_running_until_final_source_verification(tmp_path, monkeypatch):
    from apps.oa.materials.progress import progress

    image = tmp_path / 'source.png'
    Image.new('RGB', (3, 2), 'red').save(image)
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-key')
    events = []

    def post(body, **kwargs):
        Image.new('RGB', (3, 2), 'blue').save(image)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'fixture answer'}}]}

    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(files=[image], output_dir=tmp_path / 'report', on_progress=events.append)

    assert events[-2]['status'] == 'running'
    assert (events[-2]['completed_parts'], events[-2]['planned_parts']) == (1, 1)
    assert events[-1]['status'] == 'failed'
    assert events[-1]['error_code'] == 'attachment_source_changed'
    assert progress(Path(report['report_file'])) == events[-1]
    assert len(report['parts']) == 1


@pytest.mark.parametrize(('selection', 'coverage', 'expected_selection'), [
    ({'resource_id': 'original'}, 'selected_resource', {'resource_id': 'original'}),
    ({'resource_id': 'original', 'page': 1}, 'selected_page', {'resource_id': 'original', 'page': 1}),
    ({'resource_id': 'original', 'page': 1, 'region': [0, 0, 2, 1]},
     'image_region', {'resource_id': 'original', 'page': 1}),
])
def test_default_report_location_and_selected_scope_are_available_early(
        tmp_path, monkeypatch, selection, coverage, expected_selection):
    import hashlib

    from apps.oa.materials.progress import progress

    image = tmp_path / 'source.png'
    Image.new('RGB', (3, 2), 'red').save(image)
    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps({'prepared': {
        'images': [{'resource_id': 'original', 'path': str(image),
                    'content_sha256': hashlib.sha256(image.read_bytes()).hexdigest()}],
        'manifest': [], 'structured_contents': []}}))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'fake-key')
    events = []

    def post(body, **kwargs):
        early = events[0]
        path = Path(early['report_file'])
        assert path.parent.parent == tmp_path / 'recognitions'
        assert progress(path)['status'] == 'running'
        assert early['coverage'] == coverage
        assert early['selection'] == expected_selection
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'fixture answer'}}]}

    monkeypatch.setattr(recognition, '_post', post)
    report = recognition.recognize(material_file=material_file, **selection, on_progress=events.append)
    summary = progress(Path(report['report_file']))
    assert summary['status'] == 'complete'
    assert summary['coverage'] == coverage
    assert summary['selection'] == expected_selection
    if 'region' in selection:
        assert summary['region'] == [0, 0, 2, 1]
    assert (summary['completed_parts'], summary['planned_parts']) == (1, 1)
    assert len(report['parts']) == 1


def test_official_empty_progress_is_skipped_without_model(tmp_path, monkeypatch):
    from apps.oa.materials.progress import progress

    material_file = tmp_path / 'material.json'
    material_file.write_text(json.dumps({
        'resources': [], 'prepared': {'images': [], 'manifest': [], 'structured_contents': []},
        'attachment_presence': {'status': 'confirmed_empty', 'evidence_refs': ['material:declared_fields[0]']}}))

    def refuse(*args, **kwargs):
        raise AssertionError('unexpected_model_request')

    monkeypatch.setattr(recognition, '_post', refuse)
    report = recognition.recognize(material_file=material_file, output_dir=tmp_path / 'report')
    assert progress(Path(report['report_file'])) == {
        'status': 'skipped', 'report_file': report['report_file'], 'completed_parts': 0, 'planned_parts': 0,
        'coverage': 'no_attachments', 'selection': None, 'reason': 'official_no_attachments'}


def test_progress_cli_does_not_import_model_processors_or_credentials(tmp_path):
    import subprocess
    import sys

    source = tmp_path / 'source.txt'
    source.write_text('synthetic confidential contents')
    report = recognition.recognize(files=[source], output_dir=tmp_path / 'report')
    report_file = Path(report['report_file'])
    before = report_file.read_bytes()
    code = '''import importlib.abc
import sys
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'fitz', 'PIL', 'apps.oa.materials.recognize', 'apps.oa.approvals.sources.auth'}:
            raise AssertionError('unexpected_import:' + fullname)
sys.meta_path.insert(0, Refuse())
from apps.oa.materials.cli import main
raise SystemExit(main(['progress', sys.argv[1]]))
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(report_file)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'complete'
    assert 'synthetic confidential contents' not in result.stdout
    assert report_file.read_bytes() == before
