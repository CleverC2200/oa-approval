from __future__ import annotations

import errno
import os
from pathlib import Path
import stat

from .paths import PROJECT_ROOT

REQUIRED_AUTH_ENV = ("OA_BASE_URL", "OA_APP_ID", "OA_APP_SECRET", "OA_APP_PUBLIC_KEY", "OA_UID")


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


def env(name: str, default: str = "") -> str:
    load_local_env()
    return str(os.environ.get(name) or default).strip()

