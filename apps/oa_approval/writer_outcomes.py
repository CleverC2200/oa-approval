from __future__ import annotations


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
