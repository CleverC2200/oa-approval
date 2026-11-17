import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def build_report(case):
    run_id = json.loads((case / 'checkpoint.json').read_text())['run_id']
    rows = []
    for delivery_file in sorted(case.glob('delivery-*.json')):
        delivery = json.loads(delivery_file.read_text())
        task_id = delivery['task_id']
        collected_file = case / f'collect-{task_id}.json'
        collected = json.loads(collected_file.read_text())
        preserved_file = case / f'preserved-{task_id}.json'
        manifest_path = Path(json.loads(preserved_file.read_text())['manifest'])
        manifest = json.loads(manifest_path.read_text())
        if not (delivery['run_id'] == collected['run_id'] == manifest['run_id'] == run_id
                and manifest['task_id'] == collected['task_id'] == task_id
                and manifest['review_file'] == delivery['review_file']
                and collected['material_file'] == delivery['material_file']):
            raise ValueError('timing_delivery_identity_mismatch')
        files = {item['original']: item for item in manifest['files']}

        def artifact(key):
            record = files[delivery[key]]
            path = Path(record['preserved'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
                raise ValueError('timing_preserved_artifact_changed')
            return path

        review = artifact('review_file')
        opinion = artifact('opinion_file')
        receipt_file = case / f'submit-{task_id}.json'
        receipt = json.loads(receipt_file.read_text()) if receipt_file.exists() else None
        if receipt is not None and (receipt['task_id'] != task_id or receipt['status'] != 'submitted'
                or not all(receipt[key] is True for key in ('submit_ok', 'done_hit', 'postcheck_done_hit'))):
            raise ValueError('timing_submission_not_verified')
        recognition = json.loads(artifact('recognition_file').read_text()) if delivery.get('recognition_file') else None
        recognition_seconds = None
        if recognition and recognition.get('created_at') and recognition.get('completed_at'):
            recognition_seconds = (datetime.fromisoformat(recognition['completed_at'])
                                   - datetime.fromisoformat(recognition['created_at'])).total_seconds()
        ready = collected_file.stat().st_mtime
        rows.append({
            'task_id': task_id,
            'request_id': collected['request_id'],
            'workflow_key': collected['workflow_key'],
            'recognition_status': recognition.get('status') if recognition else None,
            'recognition_seconds': recognition_seconds,
            'collected_metadata_saved_at': datetime.fromtimestamp(ready, timezone.utc).isoformat(),
            'review_last_saved_at': datetime.fromtimestamp(review.stat().st_mtime, timezone.utc).isoformat(),
            'material_ready_to_review_saved_seconds': round(review.stat().st_mtime - ready, 2),
            'material_ready_to_opinion_saved_seconds': round(opinion.stat().st_mtime - ready, 2),
            'pre_submit_to_verified_receipt_seconds': round(receipt_file.stat().st_mtime - preserved_file.stat().st_mtime, 2) if receipt else None,
            'submit_receipt_saved_at': datetime.fromtimestamp(receipt_file.stat().st_mtime, timezone.utc).isoformat() if receipt else None,
            'delivery_file': str(delivery_file),
            'preservation_manifest': str(manifest_path),
        })
    if not rows:
        raise ValueError('timing_deliveries_missing')
    return {'run_id': run_id, 'tasks': rows, 'limits': [
        'Local file timestamps approximate artifact readiness, not native final delivery or request dispatch.',
        'Recognition overlaps business reading; elapsed intervals cannot be added as serial work.',
        'Preserved artifact timestamps require the existing copy2 preservation contract.',
    ]}


def main():
    parser = argparse.ArgumentParser(description='只读交付路径、保全清单和回执，不连接OA')
    parser.add_argument('--case-dir', required=True, type=Path, help='包含delivery、collect、preserved和submit记录的私有目录')
    args = parser.parse_args()
    print(json.dumps(build_report(args.case_dir), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
