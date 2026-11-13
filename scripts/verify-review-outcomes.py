import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description='Verify review outcomes through an isolated synthetic CLI owner; no network or real approvals.')
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if (root / 'approval.sqlite3').exists():
        parser.error('Use a new synthetic runtime; existing approval databases are protected')
    root.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    command = [sys.executable, '-m', 'apps.oa.approvals', '--root', str(root)]
    events = []

    def invoke(*arguments, expected=0):
        result = subprocess.run([*command, *arguments], cwd=project, capture_output=True, text=True, timeout=30)
        assert result.returncode == expected, result.stdout + result.stderr
        value = json.loads(result.stdout)
        path = root / f'event-{len(events):02}-{arguments[0]}.json'
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        events.append(str(path))
        return value

    owner = subprocess.Popen([*command, 'start', '--demo', '--batch-size', '3'], cwd=project,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        started = json.loads(owner.stdout.readline())
        (root / 'start.json').write_text(json.dumps(started, indent=2))
        task = invoke('collect', '--agent-id', 'synthetic-travel', '--expected-task-id', '1',
                      '--agent-workflow-key', 'travel_expense')
        opinion = root / 'synthetic-opinion.txt'
        opinion.write_text('Synthetic review only; unknown fixture text retained for manual judgment.')
        rejected = invoke('submit', '--task-id', '1', '--opinion-file', str(opinion),
                          '--confirm-write', 'YES', expected=1)
        assert rejected['error'] == 'review_assessment_required'
        material = Path(task['material_file'])
        conflict = material.parent / 'synthetic-reread-conflict.json'
        conflict.write_text('{"fixture_only":true,"reading_a":"1","reading_b":"7"}')

        def review_file(task_id, state, files):
            shown = invoke('show', '--task-id', str(task_id))
            value = {'task_id': task_id, 'material_sha256': shown['task']['material_sha256'],
                     'status': state, 'reason': 'Synthetic completed review with fixture uncertainty',
                     'rule_basis': 'Synthetic rule allows an unknown value', 'evidence_files': files}
            path = root / f'review-{task_id}.json'
            path.write_text(json.dumps(value))
            return str(path)

        review = review_file(1, 'business_pending', [str(material), str(conflict)])
        submitted = invoke('submit', '--task-id', '1', '--opinion-file', str(opinion),
                           '--review-file', review, '--confirm-write', 'YES')
        assert all(submitted[k] is True for k in ['submit_ok', 'done_hit', 'postcheck_done_hit'])
        assert material.exists() and conflict.exists()
        stop = root / 'invalid-stop.json'
        stop.write_text(json.dumps({'kind': 'material_read_failure', 'task_id': 1, 'reason': 'Synthetic business unknown'}))
        refused = invoke('finish', '--run-id', started['run_id'], '--require-drained', '--stop-file', str(stop), expected=1)
        assert refused['error'] == 'stop_read_failure_not_recorded'
        assert owner.poll() is None
        next_task = invoke('collect', '--agent-id', 'synthetic-entertainment', '--expected-task-id', '2',
                           '--agent-workflow-key', 'business_entertainment')
        failure = review_file(2, 'technical_failure', [next_task['material_file']])
        invoke('fail', '--task-id', '2', '--reason', 'synthetic_read_failure', '--review-file', failure)
        stop.write_text(json.dumps({'kind': 'material_read_failure', 'task_id': 2, 'reason': 'Synthetic recorded read failure'}))
        invoke('finish', '--run-id', started['run_id'], '--require-drained', '--handoff', '--stop-file', str(stop))
        owner.wait(timeout=10)
        assert owner.returncode == 0, owner.stderr.read()
        status = invoke('status', '--run-id', started['run_id'], '--view', 'batch')
        execution = status['execution']
        assert execution['status'] == 'partial_failure'
        assert (execution['verified_count'], execution['failure_count'], execution['unclaimed_count']) == (1, 1, 1)
        assert execution['headline'].startswith('❌ 部分失败') and not execution['archive_eligible']
        assert status['owner']['reachable'] is False
        assert material.exists() and conflict.exists() and Path(next_task['material_file']).exists()
        assert invoke('cleanup')['files'] == 0
        proof = {'synthetic_only': True, 'run_id': started['run_id'], 'execution': execution,
                 'business_unknown_refill': True, 'failed_stop_rejected': True,
                 'failure_evidence_preserved': True, 'owner_exited': True, 'events': events}
        (root / 'proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2))
        print(json.dumps({'status': 'passed', 'proof_file': str(root / 'proof.json')}, ensure_ascii=False))
    finally:
        if owner.poll() is None:
            owner.stdin.close()
            owner.wait(timeout=30)
        owner.stdout.close()
        owner.stderr.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
