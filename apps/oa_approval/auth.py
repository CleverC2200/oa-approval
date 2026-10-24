from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict

from apps.oa_approval import safe_http

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 30
REQUIRED_AUTH_ENV = ("OA_BASE_URL", "OA_APP_ID", "OA_APP_SECRET", "OA_APP_PUBLIC_KEY", "OA_UID")


class AuthFailure(RuntimeError):
    def __init__(self, code: str, diagnostic: dict) -> None:
        super().__init__(code)
        self.counts = {}
        self.diagnostics = [diagnostic]


def stable_env_file() -> Path:
    configured = str(os.environ.get("OA_AUTH_ENV_FILE") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
    else:
        config_home = str(os.environ.get("XDG_CONFIG_HOME") or "").strip()
        root = (
            Path(config_home).expanduser()
            if config_home
            else Path.home() / ".config"
        )
        candidate = root / "oa-workspace-minimal" / "oa-auth.env"
    if not candidate.is_absolute():
        raise RuntimeError("oa_auth_file_outside_worktree_required")
    resolved = candidate.resolve(strict=False)
    project_root = PROJECT_ROOT.resolve(strict=False)
    if resolved == project_root or project_root in resolved.parents:
        raise RuntimeError("oa_auth_file_outside_worktree_required")
    return candidate


def env_files() -> tuple[tuple[str, Path], ...]:
    return (
        ("stable", stable_env_file()),
        ("worktree", PROJECT_ROOT / ".env"),
        ("worktree", PROJECT_ROOT / ".env.oa-auth"),
    )


def _read_private_env(path: Path) -> list[str] | None:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and path.is_symlink():
        raise RuntimeError("oa_auth_file_permissions_invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RuntimeError("oa_auth_file_permissions_invalid") from None
        raise
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise RuntimeError("oa_auth_file_permissions_invalid")
        return stream.read().splitlines()


def load_local_env() -> None:
    if getattr(load_local_env, "_loaded", False):
        return
    sources = {
        name: "process" for name in REQUIRED_AUTH_ENV if str(os.environ.get(name) or "").strip()
    }
    for source, path in env_files():
        lines = _read_private_env(path)
        if lines is None:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and value and key not in os.environ:
                os.environ[key] = value
                if key in REQUIRED_AUTH_ENV and value:
                    sources[key] = source
    setattr(load_local_env, "_sources", sources)
    setattr(load_local_env, "_loaded", True)


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


def env(name: str, default: str = "") -> str:
    load_local_env()
    return str(os.environ.get(name) or default).strip()


def host() -> str:
    value = env("OA_BASE_URL").rstrip("/")
    if not value:
        raise RuntimeError("缺少 OA_BASE_URL")
    return value


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


def credentials(oa_uid: str = "") -> tuple[str, str]:
    try:
        base_url = host()
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
