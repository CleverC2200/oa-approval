from __future__ import annotations

import os
from pathlib import Path
import socket
import tempfile

os.environ['OA_AUTH_ENV_FILE'] = str(Path(tempfile.gettempdir()) / f'oa-test-no-auth-{os.getpid()}.env')

import pytest


@pytest.fixture(autouse=True)
def offline_network(monkeypatch, tmp_path):
    def reject(*args, **kwargs):
        pytest.fail('offline_network_forbidden: provide an explicit fake transport', pytrace=False)

    monkeypatch.setenv('OA_AUTH_ENV_FILE', str(tmp_path / 'no-credentials.env'))
    monkeypatch.setenv('OA_BASE_URL', 'https://oa.example.test')
    monkeypatch.setenv('OA_PROJECT_SPACE_ROOT', str(tmp_path / 'materials'))
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CACHE_DB', str(tmp_path / 'cache.sqlite3'))
    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
        monkeypatch.setattr(socket, name, reject)
    for name in ('connect', 'connect_ex', 'sendto', 'sendmsg'):
        original = getattr(socket.socket, name, None)
        if original is None:
            continue

        def guarded(channel, *args, _original=original, **kwargs):
            if channel.family in (socket.AF_INET, socket.AF_INET6):
                reject()
            return _original(channel, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)
