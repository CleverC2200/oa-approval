import json

from tests.approvals.test_hourly import invoke


def test_archive_plan_retains_only_latest_three_abnormal_rounds_and_protects_live_chats(tmp_path):
    def round_at(hour, task, **changes):
        return {'thread_id': task, 'triggered_at': f'2026-11-23T{hour:02}:00:00+08:00',
                'abnormal': True, 'archived': False, 'finished': True,
                'evidence_saved': True, 'needs_user': False} | changes

    rounds = [
        round_at(9, 'current'),
        round_at(13, 'manual', archived=True),
        round_at(11, 'old-finished'),
        round_at(14, 'newest'),
        round_at(12, 'third'),
        round_at(10, 'pending-user', needs_user=True),
        round_at(16, 'healthy-skip', abnormal=False),
        round_at(8, 'still-running', finished=False),
        round_at(7, 'missing-evidence', evidence_saved=False),
        round_at(6, 'already-archived', archived=True),
    ]
    file = tmp_path / 'history.json'
    file.write_text(json.dumps({'complete': True, 'rounds': rounds}))
    args = ('anomaly-archive-plan', '--history-file', str(file), '--current-thread-id', 'current')
    assert invoke(tmp_path, *args) == {
        'status': 'ready', 'retained_thread_ids': ['newest', 'third'],
        'archive_thread_ids': ['old-finished'],
        'protected_thread_ids': ['pending-user', 'current', 'still-running', 'missing-evidence'],
    }
    assert not (tmp_path / 'approval.sqlite3').exists()
    assert invoke(tmp_path, *args[:-1], ' old-finished ', exit_code=1)['error'] == 'chat_history_invalid'
    file.write_text(json.dumps({'complete': False, 'rounds': rounds}))
    assert invoke(tmp_path, *args, exit_code=1)['error'] == 'chat_history_incomplete'
    file.write_text(json.dumps({'complete': True, 'rounds': [*rounds, rounds[0]]}))
    assert invoke(tmp_path, *args, exit_code=1)['error'] == 'chat_history_invalid'
