from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias


@dataclass(frozen=True, slots=True)
class DownloadError:
    code: str
    message: str

    def to_record(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class Downloaded:
    path: Path


@dataclass(frozen=True, slots=True)
class Unavailable:
    pass


@dataclass(frozen=True, slots=True)
class Failed:
    error: DownloadError


TransferOutcome: TypeAlias = Downloaded | Unavailable | Failed


OA_SESSION_EXPIRED_CODE = "oa_session_expired"
ATTACHMENT_ACCESS_DENIED_CODE = "attachment_access_denied"

OA_SESSION_EXPIRED_MARKERS = (
    "ddcodetimeout",
    "login_required",
    "loginrequired",
    "/login",
    "login.jsp",
    "登录态失效",
    "登录超时",
)


def detect_oa_download_fail_code(value: Any) -> str:
    text = str(value or "")
    lowered = text.lower()
    if any(
        marker in lowered or marker in text for marker in OA_SESSION_EXPIRED_MARKERS
    ):
        return OA_SESSION_EXPIRED_CODE
    if "noright.jsp" in lowered or "无权限" in text:
        return ATTACHMENT_ACCESS_DENIED_CODE
    return ""
