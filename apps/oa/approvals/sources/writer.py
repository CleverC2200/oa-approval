from __future__ import annotations

import html
from datetime import datetime, timezone
import time
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

from apps.oa.approvals.sources import auth
from apps.oa.approvals.sources import http as safe_http
from apps.oa.approvals.sources.oa import OaApi
from apps.oa.approvals.sources.oa import TIMEOUT
from apps.oa.approvals.sources.oa import _decode_json




SUBMIT_REJECTED = "writer_submit_rejected"
TRANSPORT_UNKNOWN = "writer_transport_unknown"
POSTCHECK_TIMEOUT = "writer_postcheck_timeout"
PENDING_CONFLICT = "writer_pending_conflict"
PREFLIGHT_FAILED = "writer_preflight_failed"
TARGET_CHANGED = "submission_target_changed"

CONFIRMED_NO_WRITE_CODES = frozenset(
    {
        SUBMIT_REJECTED,
        PREFLIGHT_FAILED,
        TARGET_CHANGED,
    }
)

FAILURE_CODES = frozenset(
    {
        *CONFIRMED_NO_WRITE_CODES,
        TRANSPORT_UNKNOWN,
        POSTCHECK_TIMEOUT,
        PENDING_CONFLICT,
    }
)

UNKNOWN_WRITE_CODES = frozenset({
    TRANSPORT_UNKNOWN, POSTCHECK_TIMEOUT, PENDING_CONFLICT, "writer_result_unknown",
})


def verification_result(done_hit, pending_hit, *, submit_ok=None, fields_match=None):
    completed = done_hit and not pending_hit
    result = {
        "submit_ok": completed if submit_ok is None else submit_ok,
        "done_hit": done_hit,
        "postcheck_done_hit": completed,
    }
    if fields_match is not None:
        result["fields_match"] = fields_match
        result["postcheck_done_hit"] = completed and fields_match
    return result


def submission_status(result):
    if all(result.get(key) is True for key in ("submit_ok", "done_hit", "postcheck_done_hit")):
        return "submitted"
    return "failed" if result.get("failure_code") == SUBMIT_REJECTED else "unknown"


SUBMIT_ENDPOINT = "/api/workflow/paService/submitRequest"
POSTCHECK_ATTEMPTS = 3
POSTCHECK_INTERVAL_SECONDS = 0.25
SUBMIT_REJECTION_CODES = frozenset({"ERROR"})


def handled_since(rows, request_id, started_at):
    started = datetime.fromisoformat(started_at).astimezone(timezone.utc).replace(microsecond=0)
    for row in rows:
        value = str(row.get("handled_at") or "")
        if str(row.get("request_id")) != str(request_id) or ":" not in value:
            continue
        try:
            handled = datetime.fromisoformat(value)
        except ValueError:
            continue
        if handled.tzinfo is None:
            handled = handled.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        if handled.astimezone(timezone.utc) >= started:
            return True
    return False


def _format_remark(remark: str) -> str:
    normalized = str(remark or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return "<br/>".join(
        html.escape(line.strip(), quote=False) if line.strip() else "&nbsp;"
        for line in normalized.split("\n")
    )


class FrozenWriterApi(OaApi):
    def _post_submit(self, request_id: str, remark: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{SUBMIT_ENDPOINT}",
            data=urllib.parse.urlencode(
                {
                    "requestId": int(str(request_id)),
                    "remark": _format_remark(remark),
                }
            ).encode("utf-8"),
            headers={
                **self._headers(),
                "content-type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with safe_http.urlopen(request, timeout=TIMEOUT) as response:
            return _decode_json(response.read().decode("utf-8", errors="ignore"))

    def submit_and_verify(self, request_id: str, remark: str) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            response = self._post_submit(request_id, remark)
        except Exception:
            return {**verification_result(False, False, submit_ok=False),
                    "failure_code": TRANSPORT_UNKNOWN}
        response_code = str(response.get("code") or "").strip().upper()
        if response_code != "SUCCESS":
            failure = SUBMIT_REJECTED if response_code in SUBMIT_REJECTION_CODES else TRANSPORT_UNKNOWN
            return {**verification_result(False, False, submit_ok=False), "failure_code": failure}
        target = str(request_id)
        for attempt in range(POSTCHECK_ATTEMPTS):
            try:
                done_hit = handled_since(self.handled_rows(),target,started_at)
                pending_hit = any(
                    row.get("request_id") == target for row in self.pending_rows()
                )
            except Exception:
                break
            result = verification_result(done_hit, pending_hit, submit_ok=True)
            if done_hit:
                return {**result, "failure_code": None if result["postcheck_done_hit"] else PENDING_CONFLICT}
            if attempt + 1 < POSTCHECK_ATTEMPTS:
                time.sleep(POSTCHECK_INTERVAL_SECONDS)
        return {**verification_result(False, False, submit_ok=True),
                "failure_code": POSTCHECK_TIMEOUT}


def build_writer_api() -> FrozenWriterApi:
    return FrozenWriterApi(*auth.credentials())
