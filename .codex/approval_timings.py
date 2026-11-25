import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sys


def seconds(started, ended):
    if not started or not ended:
        return None
    try:
        elapsed = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
    except (ValueError, TypeError):
        return None
    return round(elapsed, 2) if elapsed >= 0 else None


def build_report(store, run_id):
    from apps.oa.approvals.receipts import assess_submission

    run = store.run(run_id)
    timings = json.loads(run['summary_json']).get('artifact_timings', {})
    rows = []
    for review in store.connection.execute("""SELECT r.id,r.task_id,r.created_at,r.material_sha256,
            t.request_id,t.workflow_key FROM reviews r JOIN tasks t ON t.id=r.task_id
            WHERE r.run_id=? AND r.id=(SELECT MAX(id) FROM reviews WHERE run_id=r.run_id AND task_id=r.task_id)
            ORDER BY r.task_id""", (run_id,)):
        timing = timings.get(str(review['task_id']), {})
        if timing.get('material_sha256') != review['material_sha256']:
            timing = {}
        recognition = timing.get('recognition', {})
        submission = store.connection.execute("""SELECT * FROM submissions WHERE review_id=?
            ORDER BY id DESC LIMIT 1""", (review['id'],)).fetchone()
        verified = submission is not None and assess_submission(submission, workflow_key=review['workflow_key']).verified
        rows.append({key: review[key] for key in ('task_id', 'request_id', 'workflow_key')} | {
            'material_ready_at': timing.get('material_ready_at'), 'review_saved_at': review['created_at'],
            'material_ready_to_review_saved_seconds': seconds(timing.get('material_ready_at'), review['created_at']),
            'recognition_status': recognition.get('status'),
            'recognition_seconds': seconds(recognition.get('created_at'), recognition.get('completed_at')),
            'submission_status': submission['status'] if submission else None,
            'submission_verified': verified,
            'pre_submit_to_verified_receipt_seconds': seconds(submission['started_at'], submission['completed_at'])
            if verified else None,
        })
    if not rows:
        raise ValueError('timing_deliveries_missing')
    return {'run_id': run_id, 'tasks': rows}


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from apps.oa.approvals.store import Store

    parser = argparse.ArgumentParser(description='从审批账本查询本轮耗时')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    store = Store(args.root / 'approval.sqlite3', read_only=True)
    with closing(store.connection), store.connection:
        store.connection.execute('BEGIN')
        result = build_report(store, args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
