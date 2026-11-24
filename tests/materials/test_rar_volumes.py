from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest

from apps.oa.materials import archives, cli
from tests.rar_fixtures import (
    FIRST, LAST, MEMBER_OVER_LIMIT, SINGLE, TOTAL_OVER_LIMIT, WRONG_LAST,
    stored_rar5_archive, stored_rar5_volumes,
)


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
def test_materials_prepare_and_read_keep_complete_rar_volume_originals(tmp_path, capsys):
    first, last = (tmp_path / f'evidence.part{number}.rar' for number in (1, 2))
    first.write_bytes(FIRST)
    last.write_bytes(LAST)
    manifest = tmp_path / 'sources.json'
    manifest.write_text(json.dumps({'resources': [
        {'resource_id': 'second', 'path': str(last)},
        {'resource_id': 'first', 'path': str(first)},
    ]}))
    assert cli.main(['prepare', '--manifest', str(manifest), '--output-dir', str(tmp_path / 'output')]) == 0
    result = json.loads(capsys.readouterr().out)
    material = json.loads(Path(result['material_file']).read_text())
    assert [item['resource_id'] for item in material['resources']] == ['second', 'first']
    assert [item['path'] for item in material['resources']] == [str(last), str(first)]
    second, first_item = material['resources']
    assert first_item['archive_volume_set'] == second['archive_volume_set'] == {
        'backend': '7zz', 'format': 'Rar5', 'volume_resource_ids': ['first', 'second'],
    }
    assert first_item['extracted_files'][0]['archive_path'] == 'manifest.xml'
    assert material['prepared']['structured_contents'][0]['parent_resource_id'] == 'first'
    assert cli.main(['read', result['material_file'], '--format', 'json']) == 0
    assert '73.12' in json.loads(capsys.readouterr().out)['text']
    assert [first.read_bytes(), last.read_bytes()] == [FIRST, LAST]


def _failed_prepare(capsys, output, named_paths):
    command = ['prepare', '--output-dir', str(output)]
    for path in named_paths:
        command.extend(['--file', str(path)])
    assert cli.main(command) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['error_code'] == 'material_archive_invalid'
    assert result['images'] == result['structured_contents'] == 0
    artifact = json.loads(Path(result['material_file']).read_text())
    assert artifact['diagnostics'] == result['diagnostics']
    assert artifact['status'] == 'failed'
    assert [item['path'] for item in artifact['resources']] == [str(path) for path in named_paths]
    assert not [path for path in output.rglob('*') if path.is_file() and path.name != 'material.json']
    return {item['reason'] for item in result['diagnostics']}


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
@pytest.mark.parametrize('scenario, reason', [
    ('missing_first', 'rar_volume_missing'),
    ('missing_final', 'rar_volume_missing'),
    ('damaged_header', 'rar_volume_header_invalid'),
    ('damaged_data', 'rar_volume_integrity_failed'),
    ('same_name_wrong_set', 'rar_volume_integrity_failed'),
    ('incorrect_ordinal', 'rar_volume_group_mismatch'),
    ('different_sources', 'rar_volume_missing'),
])
def test_invalid_rar_volume_sets_preserve_originals_and_fail_before_preparation(tmp_path, capsys, scenario, reason):
    first = tmp_path / 'evidence.part1.rar'
    last = tmp_path / 'evidence.part2.rar'
    first_bytes, last_bytes = FIRST, LAST
    if scenario == 'same_name_wrong_set':
        last_bytes = WRONG_LAST
    elif scenario == 'damaged_data':
        last_bytes = LAST[:200] + bytes([LAST[200] ^ 1]) + LAST[201:]
    elif scenario == 'damaged_header':
        last_bytes = LAST[:8] + bytes([LAST[8] ^ 1]) + LAST[9:]
    elif scenario == 'incorrect_ordinal':
        last = tmp_path / 'evidence.part3.rar'
    elif scenario == 'different_sources':
        second_source = tmp_path / 'second-source'
        second_source.mkdir()
        last = second_source / last.name
    first.write_bytes(first_bytes)
    last.write_bytes(last_bytes)
    paths = {'missing_first': [last], 'missing_final': [first]}.get(scenario, [first, last])
    assert reason in _failed_prepare(capsys, tmp_path / 'output', paths)
    assert [first.read_bytes(), last.read_bytes()] == [first_bytes, last_bytes]


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
def test_matching_boundary_member_cannot_verify_other_files_in_mixed_rar_volumes(tmp_path, capsys, monkeypatch):
    def invoice(amount):
        return f'<Invoice><Amount>{amount}</Amount></Invoice>'.encode()

    boundary = invoice('9.25') + b' ' * 1000
    first_bytes, _ = stored_rar5_volumes('shared.xml', boundary, preceding=[('first.xml', invoice('1.01'))],
                                         following=[('last.xml', invoice('1.02'))])
    _, last_bytes = stored_rar5_volumes('shared.xml', boundary, preceding=[('first.xml', invoice('2.01'))],
                                        following=[('last.xml', invoice('2.02'))])
    first, last = (tmp_path / f'evidence.part{number}.rar' for number in (1, 2))
    first.write_bytes(first_bytes)
    last.write_bytes(last_bytes)
    commands = _trace_7zz(tmp_path, monkeypatch)

    assert _failed_prepare(capsys, tmp_path / 'output', [first, last]) == {'rar_volume_multiple_members_unverified'}
    assert commands.read_text().splitlines() == ['l']
    assert [first.read_bytes(), last.read_bytes()] == [first_bytes, last_bytes]


def test_complete_rar_volume_set_requires_7zz(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    first, last = (tmp_path / f'evidence.part{index}.rar' for index in (1, 2))
    first.write_bytes(FIRST)
    last.write_bytes(LAST)
    assert _failed_prepare(capsys, tmp_path / 'output', [first, last]) == {'rar_volume_7zz_unavailable'}


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
@pytest.mark.parametrize('parts, reason', [
    (MEMBER_OVER_LIMIT, 'rar_archive_member_size_limit'),
    (TOTAL_OVER_LIMIT, 'rar_archive_total_size_limit'),
], ids=['161-miB-member', '201-miB-total'])
def test_rar_volume_limits_check_real_inventory_before_testing_or_extracting(
    tmp_path, capsys, monkeypatch, parts, reason,
):
    commands = _trace_7zz(tmp_path, monkeypatch)
    paths = []
    for index, data in enumerate(parts, 1):
        path = tmp_path / f'evidence.part{index}.rar'
        path.write_bytes(data)
        paths.append(path)
    assert _failed_prepare(capsys, tmp_path / 'output', paths) == {reason}
    assert commands.read_text().splitlines() == ['l']
    assert [path.read_bytes() for path in paths] == list(parts)


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
@pytest.mark.parametrize('total_limit', [1703, 1704], ids=['over-limit', 'exact-limit'])
def test_nested_zip_expansion_counts_container_and_member_against_rar_group_limit(tmp_path, capsys, monkeypatch, total_limit):
    with BytesIO() as payload:
        with ZipFile(payload, 'w') as nested:
            nested.writestr('manifest.xml', b'<Invoice><Amount>73.12</Amount></Invoice>' + b' ' * 750)
        parts = stored_rar5_volumes('nested.zip', payload.getvalue())
    monkeypatch.setattr(archives, 'RAR_VOLUME_MAX_TOTAL_BYTES', total_limit)
    first, last = (tmp_path / f'evidence.part{number}.rar' for number in (1, 2))
    for path, part in zip((first, last), parts):
        path.write_bytes(part)
    output = tmp_path / 'output'

    if total_limit == 1703:
        assert _failed_prepare(capsys, output, [first, last]) == {'rar_archive_total_size_limit'}
    else:
        assert cli.main(['prepare', '--file', str(first), '--file', str(last), '--output-dir', str(output)]) == 0
        result = json.loads(capsys.readouterr().out)
        material = json.loads(Path(result['material_file']).read_text())
        assert material['resources'][0]['extracted_files'][0]['archive_path'] == 'nested.zip::manifest.xml'
        assert cli.main(['read', result['material_file'], '--format', 'json']) == 0
        assert '73.12' in json.loads(capsys.readouterr().out)['text']
    assert [first.read_bytes(), last.read_bytes()] == list(parts)


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
def test_verified_rar_group_uses_7zz_and_ordinary_archives_keep_their_configured_limits(tmp_path, monkeypatch):
    commands = _trace_7zz(tmp_path, monkeypatch)
    first, last = (tmp_path / f'evidence.part{index}.rar' for index in (1, 2))
    first.write_bytes(FIRST)
    last.write_bytes(LAST)
    single = tmp_path / 'ordinary.part1.rar'
    single.write_bytes(SINGLE)
    ordinary_zip = tmp_path / 'ordinary.zip'
    with ZipFile(ordinary_zip, 'w') as archive:
        archive.writestr('manifest.xml', b'<Invoice><Amount>73.12</Amount></Invoice>' + b' ' * 3100)
    monkeypatch.setenv('OA_ATTACHMENT_ARCHIVE_MAX_FILE_BYTES', '2048')

    def prepare_with_config(name, *paths):
        arguments = ['prepare', '--output-dir', str(tmp_path / name)]
        for path in paths:
            arguments.extend(['--file', str(path)])
        completed = subprocess.run([sys.executable, '-m', 'apps.oa.materials', *arguments],
                                   capture_output=True, text=True, check=False)
        return completed.returncode, json.loads(completed.stdout)

    retcode, result = prepare_with_config('group', first, last)
    assert retcode == 0 and result['structured_contents'] == 1
    assert commands.read_text().splitlines() == ['l', 't', 'x']
    for index, path in enumerate([single, ordinary_zip]):
        retcode, result = prepare_with_config(f'ordinary-limited-{index}', path)
        assert retcode == 1 and result['error_code'] == 'material_archive_invalid'
    monkeypatch.delenv('OA_ATTACHMENT_ARCHIVE_MAX_FILE_BYTES')
    retcode, result = prepare_with_config('ordinary-normal', single)
    assert retcode == 0 and result['structured_contents'] == 1


def _trace_7zz(tmp_path, monkeypatch, *, extract=None):
    binary = shutil.which('7zz')
    commands = tmp_path / '7zz-commands'
    wrapper = tmp_path / '7zz'
    wrapper.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> {shlex.quote(str(commands))}\n'
                       + (f'if [ "$1" = x ]; then exec {extract}; fi\n' if extract else '')
                       + f'exec {shlex.quote(binary)} "$@"\n')
    wrapper.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    return commands


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
def test_rar_group_rejects_extractor_output_larger_than_the_verified_member(tmp_path, capsys, monkeypatch):
    commands = _trace_7zz(tmp_path, monkeypatch,
                          extract=f'{shlex.quote(sys.executable)} -c "import sys; sys.stdout.write(chr(65)*3155)"')
    first, last = (tmp_path / f'evidence.part{index}.rar' for index in (1, 2))
    first.write_bytes(FIRST)
    last.write_bytes(LAST)
    assert _failed_prepare(capsys, tmp_path / 'output', [first, last]) == {'rar_archive_output_mismatch'}
    assert commands.read_text().splitlines() == ['l', 't', 'x']
    assert [first.read_bytes(), last.read_bytes()] == [FIRST, LAST]


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
def test_rar_group_times_out_and_does_not_publish_partial_children(tmp_path, monkeypatch):
    commands = _trace_7zz(tmp_path, monkeypatch, extract='/bin/sleep 3')
    first, last = (tmp_path / f'evidence.part{index}.rar' for index in (1, 2))
    first.write_bytes(FIRST)
    last.write_bytes(LAST)
    monkeypatch.setenv('OA_ATTACHMENT_ARCHIVE_EXTRACT_TIMEOUT_SEC', '1')
    output = tmp_path / 'output'
    completed = subprocess.run([sys.executable, '-m', 'apps.oa.materials', 'prepare', '--file', str(first),
                                '--file', str(last), '--output-dir', str(output)], capture_output=True, text=True)
    assert completed.returncode == 1
    result = json.loads(completed.stdout)
    assert {item['reason'] for item in result['diagnostics']} == {'rar_archive_timeout'}
    assert commands.read_text().splitlines() == ['l', 't', 'x']
    assert not [path for path in output.rglob('*') if path.is_file() and path.name != 'material.json']


@pytest.mark.skipif(not shutil.which('7zz'), reason='7zz is required for RAR volume integration')
def test_nested_rar_extractor_shares_the_parent_group_deadline(tmp_path, monkeypatch, capsys):
    completed_extracts = tmp_path / 'completed-extracts'
    real_7zz = shlex.quote(shutil.which('7zz'))
    slow_extract = (f'/bin/sleep 1.1\nprintf "complete\\n" >> {shlex.quote(str(completed_extracts))}\n'
                    f'exec {real_7zz} "$@"')
    commands = _trace_7zz(tmp_path, monkeypatch, extract=f'/bin/sh -c {shlex.quote(slow_extract)} sh "$@"')
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(archives, 'ARCHIVE_EXTRACT_TIMEOUT_SEC', 2)
    first, last = (tmp_path / f'evidence.part{number}.rar' for number in (1, 2))
    nested = stored_rar5_archive('manifest.xml', b'<Invoice><Amount>73.12</Amount></Invoice>')
    parts = stored_rar5_volumes('nested.rar', nested)
    for path, part in zip((first, last), parts):
        path.write_bytes(part)

    assert _failed_prepare(capsys, tmp_path / 'output', [first, last]) == {'rar_archive_timeout'}
    assert completed_extracts.read_text().splitlines() == ['complete']
    assert commands.read_text().splitlines() == ['l', 't', 'x', 'l', 'x']
    assert [first.read_bytes(), last.read_bytes()] == list(parts)


def test_manifest_cannot_certify_a_missing_rar_volume(tmp_path, capsys):
    first = tmp_path / 'evidence.part1.rar'
    first.write_bytes(FIRST)
    manifest = tmp_path / 'sources.json'
    manifest.write_text(json.dumps({'resources': [{
        'resource_id': 'first', 'path': str(first), 'archive_status': 'volume_verified',
        'archive_volume_set': {'backend': '7zz', 'format': 'Rar5', 'volume_resource_ids': ['first', 'missing']},
    }]}))
    assert cli.main(['prepare', '--manifest', str(manifest), '--output-dir', str(tmp_path / 'output')]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['diagnostics'] == [{'stage': 'archive_expand', 'reason': 'rar_volume_missing',
                                      'attachment_ref': 'first'}]
    saved = json.loads(Path(result['material_file']).read_text())
    assert 'archive_volume_set' not in saved['resources'][0]
    assert first.read_bytes() == FIRST
