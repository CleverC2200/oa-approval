from __future__ import annotations

import base64
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from apps.oa_approval import api, auth, overseas_invoice_api, writer_api


@pytest.fixture
def authentication(monkeypatch):
    values = {'OA_BASE_URL': 'https://oa.example.test', 'OA_APP_ID': 'fake-app',
              'OA_APP_SECRET': 'fake-secret', 'OA_UID': '77',
              'OA_APP_PUBLIC_KEY': base64.b64encode(b'fake-public-key').decode()}
    monkeypatch.setattr(auth, 'env', lambda name, default='': values.get(name, default))
    calls = []
    encrypted = []

    def encrypt(path, value):
        encrypted.append((Path(path).read_text(), value))
        return 'fake-cipher'

    def post(url, headers, body=''):
        assert url.endswith('/applytoken')
        assert headers == {'appid': 'fake-app', 'secret': 'fake-cipher'}
        calls.append(url)
        return {'token': 'fake-token', 'status': True, 'code': 0}

    monkeypatch.setattr(auth, '_openssl_encrypt', encrypt)
    monkeypatch.setattr(auth, '_http_post', post)
    return values, calls, encrypted


@pytest.mark.parametrize('factory', [lambda: api.build_api({}, oa_uid_override='88'),
                                     writer_api.build_writer_api, overseas_invoice_api.build_api])
def test_clients_reuse_configured_registration_without_registering(authentication, factory):
    _, calls, encrypted = authentication
    client = factory()
    assert client.token == 'fake-token' and client.enc_uid == 'fake-cipher'
    assert len(calls) == 1
    assert len(encrypted) == 2 and encrypted[0][0] == encrypted[1][0]
    assert encrypted[0][1] == 'fake-secret'
    assert encrypted[1][1] == ('88' if client.__class__ is api.OaApi else '77')


def test_concurrent_clients_only_apply_tokens(authentication):
    _, calls, encrypted = authentication
    with ThreadPoolExecutor(max_workers=4) as pool:
        clients = list(pool.map(lambda _: api.build_api({}), range(12)))
    assert len(clients) == len(calls) == 12
    assert len({key for key, _ in encrypted}) == 1


@pytest.mark.parametrize('public_key', ['', 'not a public key'])
def test_missing_or_invalid_registration_material_never_registers(authentication, public_key):
    values, calls, _ = authentication
    values['OA_APP_PUBLIC_KEY'] = public_key
    with pytest.raises(auth.AuthFailure, match='^oa_auth_configuration_invalid$'):
        api.build_api({})
    assert calls == []


@pytest.mark.parametrize('response,reason', [
    ({'code': -1, 'status': False, 'msg': 'secret解密失败,请检查加密内容.'}, 'secret_decryption_failed'),
    ({'code': -2, 'status': False, 'token': 'untrusted-token'}, 'token_rejected'),
    ({'data': {}}, 'token_missing'),
    ([], 'response_invalid'),
])
def test_auth_failure_is_safe_and_does_not_retry(authentication, monkeypatch, response, reason):
    calls = []

    def post(*args):
        calls.append(args)
        return response

    monkeypatch.setattr(auth, '_http_post', post)
    with pytest.raises(auth.AuthFailure, match='^oa_auth_applytoken_failed$') as caught:
        api.build_api({})
    diagnostic, = caught.value.diagnostics
    assert diagnostic['stage'] == 'applytoken' and diagnostic['reason'] == reason
    assert len(diagnostic['key_fingerprint']) == 64
    assert len(calls) == 1
    report = json.dumps(caught.value.diagnostics)
    assert all(value not in report for value in ('fake-secret', 'fake-cipher', 'untrusted-token', '请检查'))


def test_transport_error_is_redacted(authentication, monkeypatch):
    def fail(*args):
        raise RuntimeError('fake-secret fake-cipher sensitive response')

    monkeypatch.setattr(auth, '_http_post', fail)
    with pytest.raises(auth.AuthFailure) as caught:
        api.build_api({})
    assert caught.value.diagnostics[0]['reason'] == 'transport_failed'
    assert 'sensitive' not in str(caught.value)
