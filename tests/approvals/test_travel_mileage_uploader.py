import hashlib
import json
from pathlib import Path

import fitz
import pytest

from apps.oa.approvals import cli, owner
from apps.oa.approvals.demo import create_demo
from apps.oa.materials import cli as materials_cli
from apps.oa.materials import recognize
from apps.oa.materials.formats import prepare_materials


@pytest.fixture
def mileage_review(tmp_path, monkeypatch, capsys):
    app = create_demo(tmp_path)
    collect_demo = app.collector
    run = app.pending(batch_size=1)
    task = run['tasks'][0]
    assert task['workflow_key'] == 'travel_expense'
    task_id = str(task['id'])
    monkeypatch.setattr(cli, 'send', lambda _root, request: owner.dispatch(app, run['run_id'], request))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'synthetic-review-key')

    def invoke(operation, *args):
        code = cli.main(['--root', str(tmp_path), operation, *args])
        return code, json.loads(capsys.readouterr().out)

    def prepare(claimant, *readings):
        def collect(*args, **kwargs):
            material = collect_demo(*args, **kwargs)
            if claimant is not None:
                material['declared_fields'].append({'name': '报销人', 'path': 'main[0].field[2]',
                    'value': claimant, 'read_status': 'present', 'value_source': 'display'})
            if len(readings) > 1:
                resource = material['resources'][0]
                source = Path(resource['local_readonly_path'])
                with fitz.open(source) as pdf:
                    for index in range(1, len(readings)):
                        pdf.new_page().insert_text((30, 40), f'Synthetic photo {index + 1}')
                    pdf.saveIncr()
                resource['source_content_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
                material['prepared'] = prepare_materials([{'path': str(source),
                    'resource_id': resource['resource_id'], 'name': resource['name']}],
                    out_dir=kwargs['prepared_root'] / 'mileage').to_record()
            return material

        app.collector = collect
        answers = iter(readings)
        monkeypatch.setattr(recognize, '_post', lambda *_args, **_kwargs: {
            'choices': [{'finish_reason': 'stop', 'message': {'content': next(answers)}}]})
        code, collected = invoke('collect', '--expected-task-id', task_id)
        assert code == 0 and collected['status'] == 'prepared', collected
        path = Path(collected['material_file'])
        assert materials_cli.main(['recognize', '--material-file', str(path),
                                   '--output-dir', str(path.parent / 'recognized')]) == 0
        recognition = json.loads(capsys.readouterr().out)['report_file']
        code, dispatched = invoke('dispatch-review', '--expected-task-id', task_id,
            '--agent-id', 'synthetic-mileage-reviewer', '--agent-workflow-key', 'travel_expense',
            '--recognition-file', recognition)
        assert code == 0 and dispatched['status'] == 'reviewing', dispatched

        def deliver(opinion, *, observation=None, locator='第1页／水印'):
            opinion_file = path.parent / 'opinion.txt'
            opinion_file.write_text(opinion)
            evidence_files = [str(path), recognition]
            options = []
            if observation is not None:
                material = json.loads(path.read_text())
                photo = material['resources'][0]['resource_id']
                observations = path.parent / 'observations.json'
                observations.write_text(json.dumps([{'resource_id': photo, 'locator': locator,
                    'source': 'agent', 'status': 'read', 'content': observation}]))
                evidence_files.append(str(observations))
                options += ['--observations-file', str(observations)]
            assessment = path.parent / 'review.json'
            assessment.write_text(json.dumps({'task_id': task['id'], 'material_sha256': dispatched['material_sha256'],
                'status': 'business_pending', 'reason': 'Synthetic review with a pending question',
                'rule_basis': '第 6 项油补', 'evidence_files': evidence_files}))
            return invoke('deliver', '--task-id', task_id, '--agent-id', dispatched['agent_id'],
                '--material-file', str(path), '--material-sha256', dispatched['material_sha256'],
                '--opinion-file', str(opinion_file), '--review-file', str(assessment), *options)

        return deliver

    try:
        yield prepare, lambda operation: invoke(operation, '--task-id', task_id)
    finally:
        app.store.connection.close()


@pytest.mark.parametrize(('claimant', 'reading', 'claim'), [
    ('样例甲', '- 第一行：样例甲·油补里程上报(里程公里数照片)',
     '3月8日照片上传人识别为样例乙，与报销人样例甲不一致，照片归属及是否代传待核。'),
    ('测试丙', '报销人和油补照片上传人均为测试丙。',
     '照片上传人为测试丁，与报销人测试丙不一致，是否代传待核。'),
    ('样例甲', '里程照片上传人：样例甲。', '里程照片由样例乙上传，水印位置在下方。'),
])
def test_conflicting_mileage_uploader_cannot_be_delivered_or_previewed(mileage_review, claimant, reading, claim):
    prepare, inspect = mileage_review
    deliver = prepare(claimant, reading)
    independent = '⚠️ 6、油补：3月8日照片不在4月申报期间，所属期间待核。'
    code, rejected = deliver(independent + claim)
    assert code == 1 and rejected['error'] == 'travel_mileage_uploader_conflict', rejected
    code, preview = inspect('preview')
    assert code == 1 and preview['error'] == 'task_not_reviewable'
    code, details = inspect('show')
    assert code == 0 and details['task']['status'] == 'reviewing'
    assert details['reviews'] == [] and details['submissions'] == []
    assert deliver(independent)[1]['status'] == 'reviewed'
    code, preview = inspect('preview')
    assert code == 0 and preview['opinion'] == independent


@pytest.mark.parametrize(('reading', 'observation', 'uploader_claim'), [
    ('里程照片上传者：样例乙。', None, '里程照片由样例乙上传，和报销人样例甲姓名不同'),
    ('水印文字模糊。', '里程照片上传人：样例乙。', '里程照片上传人：样例乙，与报销人样例甲不同'),
])
def test_confirmed_different_mileage_uploader_and_date_can_be_previewed(mileage_review, reading, observation, uploader_claim):
    prepare, inspect = mileage_review
    opinion = (f'⚠️ 6、油补：3月8日{uploader_claim}，照片归属和代为上传待核。'
               '照片日期与4月报销期间不一致。')
    code, saved = prepare(' 样例甲 ', reading)(opinion, observation=observation)
    assert code == 0 and saved['status'] == 'reviewed', saved
    code, preview = inspect('preview')
    assert code == 0 and preview['opinion'] == opinion


@pytest.mark.parametrize('reading', [
    '只看到里程显示区，水印未见姓名。',
    '里程照片上传人：样例乙（姓名看不清）。',
    '里程照片上传人：样例乙，水印姓名看不清，只是根据轮廓猜测。',
    '里程照片上传人：样例乙。水印破损，姓名看不清，只是根据轮廓猜测。',
    '里程照片上传人：样例乙？水印模糊，无法确认。',
    '上半部是里程照片，水印未显示姓名。下半部打车发票上传人：样例乙。',
])
def test_missing_or_unreadable_mileage_name_only_preserves_independent_review(mileage_review, reading):
    prepare, inspect = mileage_review
    deliver = prepare('样例甲', reading)
    code, rejected = deliver('⚠️ 6、油补：上传人识别为样例乙，与报销人样例甲不一致，疑似代传。')
    assert code == 1 and rejected['error'] == 'travel_mileage_uploader_conflict', rejected
    opinion = '⚠️ 6、油补：照片水印姓名无法确认，照片日期与报销人行程不一致。'
    assert deliver(opinion)[1]['status'] == 'reviewed'
    code, preview = inspect('preview')
    assert code == 0 and preview['opinion'] == opinion


@pytest.mark.parametrize(('claimant', 'reading', 'opinion'), [
    ('样例甲', '里程照片上传人：样例甲。', '照片上传人与报销人姓名有差异且拍摄日期待核。'),
    ('样例甲', '3月8日里程照片上传人：样例乙。', '4月9日照片上传人与报销人不一致。'),
    (101, '里程照片上传人：样例乙。', '里程照片由样例乙上传，和报销人姓名不同。'),
])
def test_mileage_uploader_claim_requires_comparable_names(mileage_review, claimant, reading, opinion):
    prepare, _ = mileage_review
    code, rejected = prepare(claimant, reading)('⚠️ 6、油补：' + opinion)
    assert code == 1 and rejected['error'] == 'travel_mileage_uploader_conflict', rejected


def test_named_mileage_opinion_uses_the_matching_photo(mileage_review):
    prepare, inspect = mileage_review
    deliver = prepare('样例甲', '3月8日里程照片上传人：样例甲。',
                      '4月9日，里程照片上传人：样例乙。5月10日里程照片归属待核，发票日期5月10日。')
    claim = '里程照片上传人：样例乙，与报销人样例甲不一致，是否代传待核。'
    for opinion in ('3月8日' + claim, '5月10日' + claim, claim, '（material:prepared.images.demo-pdf.1）' + claim):
        code, rejected = deliver('⚠️ 6、油补：' + opinion)
        assert code == 1 and rejected['error'] == 'travel_mileage_uploader_conflict', rejected
        assert inspect('preview')[1]['error'] == 'task_not_reviewable'
    opinion = '⚠️ 6、油补：4月9日，' + claim
    assert deliver(opinion)[1]['status'] == 'reviewed'
    assert inspect('preview')[1]['opinion'] == opinion


def test_mileage_observation_requires_existing_explicit_page_locator(mileage_review):
    prepare, inspect = mileage_review
    deliver = prepare('样例甲', '水印文字模糊。')
    opinion = '⚠️ 6、油补：里程照片上传人：样例乙，与报销人样例甲不一致，是否代传待核。'
    reading = '里程照片上传人：样例乙。'
    for locator in ('material:prepared.images.demo-pdf.99#水印', '第99页／水印'):
        code, rejected = deliver(opinion, observation=reading, locator=locator)
        assert code == 1 and rejected['error'] == 'travel_mileage_uploader_conflict', rejected
    assert deliver(opinion, observation=reading, locator='material:prepared.images.demo-pdf.1#水印')[1][
        'status'] == 'reviewed'
    assert inspect('preview')[1]['opinion'] == opinion


def test_mileage_uploader_alternatives_do_not_substantiate_a_mismatch(mileage_review):
    prepare, _ = mileage_review
    code, rejected = prepare('样例甲', '里程照片上传人：样例乙或样例丙。')(
        '⚠️ 6、油补：里程照片上传人：样例乙或样例丙，与报销人样例甲不一致，照片归属待核。')
    assert code == 1 and rejected['error'] == 'travel_mileage_uploader_conflict', rejected


@pytest.mark.parametrize('opinion', [
    '照片水印显示样例乙，与报销人样例甲不是同一人，照片归属待核。',
    '上传人不是报销人本人。',
])
def test_mileage_watermark_identity_claim_cannot_disagree_with_the_photo(mileage_review, opinion):
    prepare, _ = mileage_review
    code, rejected = prepare('样例甲', '里程照片上传人：样例甲。')('⚠️ 6、油补：' + opinion)
    assert code == 1 and rejected['error'] == 'travel_mileage_uploader_conflict', rejected


@pytest.mark.parametrize(('reading', 'opinion'), [
    ('4月9日里程照片水印姓名无法确认。', '4月9日里程照片上传人无法确认且拍摄日与申报月份不一致。'),
    ('4月9日里程照片上传人：样例甲。水印日期看不清。', '里程照片上传人与报销人一致、照片水印月份与申报月份不符，所属月待核。'),
    ('4月9日里程照片上传人：样例乙。水印时间看不清。',
     '4月9日里程照片上传人：样例乙，与报销人样例甲不一致。照片归属和是否代传待核。'),
])
def test_mileage_date_and_followup_doubts_are_preserved(mileage_review, reading, opinion):
    prepare, inspect = mileage_review
    opinion = '⚠️ 6、油补：' + opinion
    code, saved = prepare('样例甲', reading)(opinion)
    assert code == 0 and saved['status'] == 'reviewed', saved
    assert inspect('preview')[1]['opinion'] == opinion


def test_mileage_observation_can_supply_the_photo_date_when_report_has_no_date(mileage_review):
    prepare, inspect = mileage_review
    deliver = prepare('样例甲', '里程照片上传人：样例甲。', '里程照片上传人：样例乙。')
    opinion = '⚠️ 6、油补：4月9日里程照片上传人：样例乙，与报销人样例甲不一致，是否代传待核。'
    assert deliver(opinion)[1]['error'] == 'travel_mileage_uploader_conflict'
    assert deliver(opinion, observation='4月9日里程照片上传人：样例乙。',
                   locator='material:prepared.images.demo-pdf.2#水印')[1]['status'] == 'reviewed'
    assert inspect('preview')[1]['opinion'] == opinion
