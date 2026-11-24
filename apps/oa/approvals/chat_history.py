from datetime import datetime


ANOMALOUS_ROUND_LIMIT = 3


def anomaly_archive_plan(history, current_thread_id):
    if not isinstance(history, dict) or history.get('complete') is not True:
        raise ValueError('chat_history_incomplete')
    rounds = history.get('rounds')
    if (not isinstance(rounds, list) or not isinstance(current_thread_id, str) or not current_thread_id
            or current_thread_id != current_thread_id.strip()):
        raise ValueError('chat_history_invalid')
    seen = set()
    anomalies = []
    for item in rounds:
        if not isinstance(item, dict):
            raise ValueError('chat_history_invalid')
        thread_id = item.get('thread_id')
        timestamp = item.get('triggered_at')
        if (not isinstance(thread_id, str) or not thread_id or thread_id != thread_id.strip()
                or thread_id in seen or not isinstance(timestamp, str)
                or any(type(item.get(key)) is not bool for key in
                       ('abnormal', 'archived', 'finished', 'evidence_saved', 'needs_user'))):
            raise ValueError('chat_history_invalid')
        try:
            triggered_at = datetime.fromisoformat(timestamp)
        except ValueError:
            raise ValueError('chat_history_invalid') from None
        if triggered_at.utcoffset() is None:
            raise ValueError('chat_history_invalid')
        seen.add(thread_id)
        if item['abnormal']:
            anomalies.append((triggered_at, thread_id, item))
    anomalies.sort(reverse=True)
    retained, archive, protected = [], [], []
    for index, (_, thread_id, item) in enumerate(anomalies):
        if item['archived']:
            continue
        if index < ANOMALOUS_ROUND_LIMIT:
            retained.append(thread_id)
        elif thread_id == current_thread_id or not item['finished'] or not item['evidence_saved'] or item['needs_user']:
            protected.append(thread_id)
        else:
            archive.append(thread_id)
    return {'status': 'ready', 'retained_thread_ids': retained,
            'archive_thread_ids': archive, 'protected_thread_ids': protected}
