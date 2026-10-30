from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Literal, NotRequired, TypedDict


SubmissionStatus = Literal["submitted", "failed", "unknown"]


class ReceiptRecord(TypedDict):
    submit_ok: bool
    done_hit: bool
    postcheck_done_hit: bool
    fields_match: NotRequired[bool]
    failure_code: NotRequired[str | None]


@dataclass(frozen=True, slots=True)
class SubmissionEvidence:
    receipt: dict[str, object]
    verified: bool
    resolved: bool


SUBMIT_REJECTED = "writer_submit_rejected"
USER_CONFIRMED_NOT_WRITTEN = "user_confirmed_not_written"
TRANSPORT_UNKNOWN = "writer_transport_unknown"
POSTCHECK_TIMEOUT = "writer_postcheck_timeout"
PENDING_CONFLICT = "writer_pending_conflict"
PREFLIGHT_FAILED = "writer_preflight_failed"
TARGET_CHANGED = "submission_target_changed"

CONFIRMED_NO_WRITE_CODES = frozenset(
    {
        SUBMIT_REJECTED,
        USER_CONFIRMED_NOT_WRITTEN,
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


def verification_result(
    done_hit: bool,
    pending_hit: bool,
    *,
    submit_ok: bool | None = None,
    fields_match: bool | None = None,
) -> ReceiptRecord:
    completed = done_hit and not pending_hit
    result: ReceiptRecord = {
        "submit_ok": completed if submit_ok is None else submit_ok,
        "done_hit": done_hit,
        "postcheck_done_hit": completed,
    }
    if fields_match is not None:
        result["fields_match"] = fields_match
        result["postcheck_done_hit"] = completed and fields_match
    return result


def submission_status(result: Mapping[str, object]) -> SubmissionStatus:
    if _has_completion_evidence(result):
        return "submitted"
    return "failed" if result.get("failure_code") in {SUBMIT_REJECTED, USER_CONFIRMED_NOT_WRITTEN} else "unknown"


def assess_submission(submission: Mapping[str, object], *, workflow_key: str) -> SubmissionEvidence:
    receipt = json.loads(submission["result_json"])
    verified = (
        submission["status"] == "submitted"
        and submission["completed_at"] is not None
        and _has_completion_evidence(receipt)
        and (receipt.get("fields_match") is True if workflow_key == "overseas_invoice"
             else receipt.get("fields_match") is not False)
    )
    confirmed_not_written = (
        submission['status'] == 'failed'
        and submission['completed_at'] is not None
        and receipt.get('failure_code') == USER_CONFIRMED_NOT_WRITTEN
        and isinstance(receipt.get('confirmation_note'), str)
        and bool(receipt['confirmation_note'].strip())
        and isinstance(receipt.get('previous_result'), dict)
    )
    return SubmissionEvidence(receipt, verified, verified or confirmed_not_written)


def _has_completion_evidence(result: Mapping[str, object]) -> bool:
    return all(result.get(key) is True for key in ("submit_ok", "done_hit", "postcheck_done_hit"))
