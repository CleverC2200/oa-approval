from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any
import re
import urllib.parse

from apps.oa.approvals.sources import travel_hotel


DISCOVERY_FAILED = "travel_attachment_discovery_failed"
PENDING_TEXT_MARKERS = ("系统正在处理中，请稍后", "正在处理中，请稍后")
ATTACHMENT_ID_RE = re.compile(r"/opera/(?:getFile|share/open)/(\d+)/", re.IGNORECASE)


def attachment_id(url: str) -> str:
    match = ATTACHMENT_ID_RE.search(urllib.parse.urlparse(url).path)
    if match:
        return urllib.parse.unquote(match.group(1))
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def page_pending(text: str) -> bool:
    return any(marker in text for marker in PENDING_TEXT_MARKERS)


def merge_inventory(target: list[dict], incoming) -> None:
    identities = {
        (
            item.get("owner_ref"),
            item.get("attachment_ref") or item.get("source_path"),
        ): item
        for item in target
    }
    for item in incoming:
        key = (
            item.get("owner_ref"),
            item.get("attachment_ref") or item.get("source_path"),
        )
        previous = identities.get(key)
        if previous is None:
            target.append(dict(item))
            identities[key] = target[-1]
        elif any(
            previous.get(field) != item.get(field) for field in ("reason", "file_ref")
        ):
            previous["reason"] = "inventory_changed"


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    target_usable: bool
    attempt_complete: bool
    diagnostics: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    text: str = ""
    download_targets: tuple[dict, ...] = ()
    scan_status: str = "read_failed"
    inventory: tuple[dict, ...] = ()
    hotel_details: tuple[dict, ...] = ()
    browser: str = ""
    diagnostics: tuple[dict, ...] = ()

    @classmethod
    def failure(
        cls,
        stage: str,
        reason: str,
        *,
        headless: bool | None = None,
        error: BaseException | None = None,
        **facts,
    ) -> CandidateObservation:
        browser = "" if headless is None else "headless" if headless else "visible"
        diagnostic = {"stage": stage, "reason": reason, **facts}
        if browser:
            diagnostic["browser"] = browser
        if error is not None:
            diagnostic["exception_type"] = type(error).__name__
        return cls(browser=browser, diagnostics=(diagnostic,))

    def assess(
        self, *, expected_marker: str, expected_resource_count: int | None
    ) -> CandidateAssessment:
        diagnostics = list(self.diagnostics)
        reason = ""
        if not self.text:
            reason = "page_empty" if not diagnostics else ""
        elif "登录" in self.text[:100]:
            reason = "login_required"
        elif expected_marker and expected_marker not in self.text:
            reason = "flow_marker_mismatch"
        elif page_pending(self.text):
            reason = "page_pending"
        usable = bool(self.text) and not reason
        if usable and self.scan_status not in {"empty", "resources_found"}:
            reason = (
                "attachment_tab_unavailable"
                if self.scan_status == "tab_unavailable"
                else "attachment_scan_unstable"
            )
        if reason:
            diagnostics.append(
                {
                    "stage": "attachment_scan" if usable else "page",
                    "reason": reason,
                    **({"browser": self.browser} if self.browser else {}),
                }
            )
        count = len(self.download_targets)
        complete = (self.scan_status == "resources_found" and count > 0) or (
            self.scan_status == "empty" and count == 0
        )
        if expected_resource_count is not None:
            complete = count == expected_resource_count and self.scan_status == (
                "empty" if expected_resource_count == 0 else "resources_found"
            )
        return CandidateAssessment(usable, usable and complete, tuple(diagnostics))


@dataclass(frozen=True, slots=True)
class TravelReadError:
    code: str
    message: str = ""
    counts: dict[str, Any] | None = None
    diagnostics: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class TravelCoverage:
    complete: bool
    counts: dict[str, Any]
    diagnostics: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class TravelEvidence:
    download_targets: tuple[dict, ...] = ()
    facts: tuple[dict, ...] = ()
    errors: tuple[TravelReadError, ...] = ()
    _scan: dict | None = None

    @classmethod
    def empty(cls) -> TravelEvidence:
        return cls()

    @classmethod
    def failure(cls, code: str, message: str, *, diagnostics=()) -> TravelEvidence:
        return cls(
            errors=(TravelReadError(code, message, diagnostics=tuple(diagnostics)),)
        )

    @classmethod
    def assemble(
        cls,
        observations,
        *,
        fields,
        request_id: str,
        flow_mark: str,
        expected_resource_count: int | None,
    ) -> TravelEvidence:
        valid, diagnostics, targets, inventory = [], [], [], []
        seen_ids, seen_bodies = set(), set()
        page_fields = []
        for observation in observations:
            assessment = observation.assess(
                expected_marker=flow_mark,
                expected_resource_count=expected_resource_count,
            )
            diagnostics.extend(assessment.diagnostics)
            if not assessment.target_usable:
                continue
            valid.append(observation)
            merge_inventory(inventory, observation.inventory)
            body = observation.text.strip()
            if body not in seen_bodies:
                seen_bodies.add(body)
                page_fields.append({"name": "商旅页面内容", "value": body})
            for item in observation.download_targets:
                url = str(item.get("downloadUrl") or "").strip()
                if not url:
                    continue
                raw_id = str(item.get("id") or attachment_id(url)).strip()
                resource_id = (
                    raw_id
                    if raw_id.startswith("travel_platform:")
                    else f"travel_platform:{raw_id}"
                )
                if resource_id in seen_ids:
                    continue
                seen_ids.add(resource_id)
                targets.append(
                    {
                        **item,
                        "id": resource_id,
                        "allowExtraDownloadHost": True,
                        "source": "travel_platform",
                        "source_request_id": str(request_id),
                        "discovery_ref": f"travel_platform:{flow_mark}",
                    }
                )
        page_fields.extend(
            travel_hotel.fields_for_details(
                fields,
                [
                    detail
                    for observation in valid
                    for detail in observation.hotel_details
                ],
            )
        )
        facts = tuple(
            {
                "source": "travel_platform",
                "name": str(item.get("name") or ""),
                "value": str(item.get("value") or ""),
                "subject_ref": str(item.get("subject_ref") or item.get("name") or ""),
            }
            for item in page_fields
            if str(item.get("name") or "").strip()
            and str(item.get("value") or "").strip()
        )
        if any(
            item.get("reason") not in {"downloadable", "empty_placeholder"}
            for item in inventory
        ):
            return cls(
                facts=facts,
                errors=(
                    TravelReadError(
                        "travel_attachment_resources_incomplete",
                        "商旅附件记录含无法读取的文件引用",
                        diagnostics=tuple(inventory),
                    ),
                ),
            )
        scan = None
        if (
            inventory
            or any(item.scan_status == "empty" for item in valid)
            and not targets
        ):
            scan = {
                "status": "resources_found" if targets else "empty",
                "source": "travel_platform",
                "source_request_id": str(request_id),
                "discovery_ref": f"travel_platform:{flow_mark}",
                **({"inventory": inventory} if inventory else {}),
            }
        if targets or any(item.scan_status == "empty" for item in valid):
            return cls(tuple(targets), facts, _scan=scan)
        return cls(
            facts=facts,
            _scan=scan,
            errors=(
                TravelReadError(
                    DISCOVERY_FAILED,
                    "商旅附件页面或附件信息页签未能完成稳定读取",
                    diagnostics=tuple(
                        diagnostics
                        or [{"stage": "browser", "reason": "no_candidate_result"}]
                    ),
                ),
            ),
        )

    def require_request_binding(self, request_id: str) -> None:
        for record in self.download_targets:
            if (
                str(record.get("source") or "") != "travel_platform"
                or str(record.get("source_request_id") or "") != request_id
                or not str(record.get("discovery_ref") or "").startswith(
                    "travel_platform:"
                )
            ):
                raise RuntimeError("resource_ownership_untrusted")
        if self._scan and (
            self._scan.get("source") != "travel_platform"
            or self._scan.get("source_request_id") != request_id
            or not str(self._scan.get("discovery_ref") or "").startswith(
                "travel_platform:"
            )
        ):
            raise RuntimeError("resource_ownership_untrusted")

    def attachment_scan_record(self) -> dict | None:
        if self._scan is None:
            return None
        return {
            key: ([dict(item) for item in value] if key == "inventory" else value)
            for key, value in self._scan.items()
        }

    def coverage(
        self,
        *,
        source_file_ids: set[str],
        source_resource_count: int,
        declared_count: int | None,
    ) -> TravelCoverage:
        inventory = (self._scan or {}).get("inventory") or []
        sightings = len(inventory) if inventory else source_resource_count
        complete = all(
            item.get("reason") == "empty_placeholder"
            or (
                item.get("reason") == "downloadable"
                and item.get("file_ref") in source_file_ids
            )
            for item in inventory
        ) and (declared_count is None or sightings >= declared_count)
        return TravelCoverage(
            complete,
            {
                "declared_attachment_count": declared_count,
                "trusted_source_sighting_count": sightings,
                "native_attachment_record_count": len(inventory),
                "empty_placeholder_count": sum(
                    item.get("reason") == "empty_placeholder" for item in inventory
                ),
                "unique_source_resource_count": source_resource_count,
            },
            tuple(inventory)
            + ({"stage": "attachment_count", "reason": "source_inventory_incomplete"},),
        )
