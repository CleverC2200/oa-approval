from contextlib import suppress
from datetime import datetime, timezone
import json
import os
import re
import signal
import subprocess

from apps.oa.runtime.environment import stable_env_file
from apps.oa.runtime.files import write_bytes


_CLI_ENV_KEYS = (
    'HOME', 'PATH', 'TMPDIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
    'SSL_CERT_FILE', 'SSL_CERT_DIR',
)


def refresh():
    path = stable_env_file().with_name('travel-reconciliation-sheet.json')
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'status': 'not_configured'}
    except (OSError, ValueError):
        return {'status': 'failed', 'error': 'travel_reconciliation_config_invalid'}
    if not isinstance(config, dict) or not all(
        isinstance(config.get(key), str) and re.fullmatch(r'[A-Za-z0-9]+', config[key])
        for key in ('spreadsheet_token', 'sheet_id')
    ) or config.get('identity') not in ('user', 'bot'):
        return {'status': 'failed', 'error': 'travel_reconciliation_config_invalid'}

    environment = {key: os.environ[key] for key in _CLI_ENV_KEYS if key in os.environ}
    try:
        with subprocess.Popen(
            ['lark-cli', 'sheets', '+table-get', '--spreadsheet-token', config['spreadsheet_token'],
             '--sheet-id', config['sheet_id'], '--as', config['identity'], '--max-chars', '250000'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, env=environment,
        ) as process:
            try:
                output, _ = process.communicate(timeout=30)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                if isinstance(error, KeyboardInterrupt):
                    raise
                return {'status': 'failed', 'error': 'travel_reconciliation_timeout'}
            if process.returncode:
                return {'status': 'failed', 'error': 'travel_reconciliation_read_failed'}
    except FileNotFoundError:
        return {'status': 'failed', 'error': 'lark_cli_not_found'}
    except OSError:
        return {'status': 'failed', 'error': 'travel_reconciliation_read_failed'}

    try:
        payload = json.loads(output)
        if payload.get('ok') is not True or payload.get('identity') != config['identity']:
            raise ValueError
        data = payload['data']
        sheet, = data['sheets']
        if data.get('truncated') or data.get('complete') is False or sheet.get('truncated'):
            raise ValueError
        columns, rows = sheet['columns'], sheet['data']
        if not isinstance(columns, list) or columns.count('工号') != 1 or not isinstance(rows, list):
            raise ValueError
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        return {'status': 'failed', 'error': 'travel_reconciliation_result_invalid'}

    employee_column = columns.index('工号')
    return {
        'status': 'loaded',
        'read_at': datetime.now(timezone.utc).isoformat(),
        'employee_rows': sum(row[employee_column] is not None and bool(str(row[employee_column]).strip()) for row in rows),
        'source': {key: config[key] for key in ('spreadsheet_token', 'sheet_id', 'identity')},
        'table': sheet,
    }


def save(root, run_id, snapshot):
    if snapshot['status'] != 'loaded':
        return snapshot
    path = root / 'travel-reconciliation' / f'{run_id}.json'
    try:
        write_bytes(path, json.dumps({'run_id': run_id, **snapshot}, ensure_ascii=False).encode('utf-8'))
    except OSError:
        return {'status': 'failed', 'error': 'travel_reconciliation_snapshot_write_failed'}
    return {'status': 'loaded', 'read_at': snapshot['read_at'], 'employee_rows': snapshot['employee_rows'],
            'snapshot_file': str(path)}
