from __future__ import annotations

from apps.oa.runtime.environment import REQUIRED_AUTH_ENV, env, load_local_env

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict

from apps.oa.approvals.sources import http as safe_http

TIMEOUT = 30


class AuthFailure(RuntimeError):
    def __init__(self, code: str, diagnostic: dict) -> None:
        super().__init__(code)
        self.counts = {}
        self.diagnostics = [diagnostic]


def configuration_status() -> dict[str, object]:
    load_local_env()
    configured = [name for name in REQUIRED_AUTH_ENV if str(os.environ.get(name) or "").strip()]
    sources = getattr(load_local_env, "_sources", {})
    active_sources = {sources.get(name, "process") for name in configured}
    source = next(iter(active_sources)) if len(active_sources) == 1 else "mixed" if active_sources else "none"
    return {
        "status": "ok" if len(configured) == len(REQUIRED_AUTH_ENV) else "missing",
        "configured": len(configured),
        "required": len(REQUIRED_AUTH_ENV),
        "source": source,
    }


def auth_pair() -> tuple[str, str]:
    app_id = env("OA_APP_ID")
    secret = env("OA_APP_SECRET")
    if not app_id or not secret:
        raise RuntimeError("缺少 OA_APP_ID/OA_APP_SECRET")
    return app_id, secret


def _http_post(url: str, headers: Dict[str, str], body: str = "") -> dict:
    req = urllib.request.Request(url=url, data=body.encode("utf-8"), headers=headers, method="POST")
    with safe_http.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _openssl_encrypt(pub_pem_path: str, plaintext: str) -> str:
    proc = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-encrypt",
            "-pubin",
            "-inkey",
            pub_pem_path,
            "-pkeyopt",
            "rsa_padding_mode:pkcs1",
        ],
        input=plaintext.encode("utf-8"),
        capture_output=True,
        check=True,
    )
    return base64.b64encode(proc.stdout).decode("utf-8")


def _pem_file_from_b64(spk_b64: str, tmp_dir: str) -> str:
    path = os.path.join(tmp_dir, "server.pem")
    chunks = [spk_b64[i : i + 64] for i in range(0, len(spk_b64), 64)]
    Path(path).write_text(
        "-----BEGIN PUBLIC KEY-----\n" + "\n".join(chunks) + "\n-----END PUBLIC KEY-----\n",
        encoding="utf-8",
    )
    return path


def credentials(oa_uid: str = "", *, base_url: str) -> tuple[str, str]:
    try:
        if not base_url:
            raise RuntimeError("缺少 OA_BASE_URL")
        app_id, secret = auth_pair()
        user_id = str(oa_uid or env("OA_UID")).strip()
        spk = env("OA_APP_PUBLIC_KEY")
        public_key = base64.b64decode(spk, validate=True)
        if not public_key or not user_id:
            raise ValueError("registration_material_missing")
    except Exception as error:
        raise AuthFailure("oa_auth_configuration_invalid", {
            "stage": "configuration", "reason": "registration_material_invalid",
            "exception_type": type(error).__name__,
        }) from None
    fingerprint = hashlib.sha256(public_key).hexdigest()
    stage = "secret_encrypt"
    try:
        with tempfile.TemporaryDirectory(prefix="oa_auth_") as tmp:
            key_file = _pem_file_from_b64(spk, tmp)
            encrypted_secret = _openssl_encrypt(key_file, secret)
            stage = "userid_encrypt"
            encrypted_user_id = _openssl_encrypt(key_file, user_id)
    except Exception as error:
        raise AuthFailure("oa_auth_encryption_failed", {
            "stage": stage, "reason": "encryption_failed", "key_fingerprint": fingerprint,
            "exception_type": type(error).__name__,
        }) from None
    try:
        response = _http_post(f"{base_url}/api/ec/dev/auth/applytoken", {"appid": app_id, "secret": encrypted_secret})
    except Exception as error:
        diagnostic = {"stage": "applytoken", "reason": "transport_failed",
                      "key_fingerprint": fingerprint, "exception_type": type(error).__name__}
        status = getattr(error, "code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            diagnostic["http_status"] = status
        raise AuthFailure("oa_auth_applytoken_failed", diagnostic) from None
    token = None
    diagnostic = {"stage": "applytoken", "reason": "response_invalid", "key_fingerprint": fingerprint}
    if isinstance(response, dict):
        code = response.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            diagnostic["api_code"] = code
        rejected = response.get("status") is False or (code is not None and code not in (0, "0"))
        data = response.get("data")
        token = response.get("token") or (data.get("token") if isinstance(data, dict) else None)
        diagnostic["reason"] = ("secret_decryption_failed" if "secret解密失败" in str(response.get("msg") or "")
                                else "token_rejected" if rejected else "token_missing")
        if rejected:
            token = None
    if not isinstance(token, str) or not token.strip():
        raise AuthFailure("oa_auth_applytoken_failed", diagnostic)
    return token, encrypted_user_id
