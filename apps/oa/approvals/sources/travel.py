from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any, Dict, List

from apps.oa.approvals.source_policy import SourcePolicy
from apps.oa.approvals.sources import downloads as attachments, travel_browser
from apps.oa.approvals.sources.travel_evidence import (
    CandidateObservation,
    TravelEvidence,
    DISCOVERY_FAILED,
)
from apps.oa.approvals import forms as evidence_packet
from apps.oa.runtime import control as runtime_control

HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def _field_value(fields: List[Dict[str, Any]], names: tuple[str, ...]) -> str:
    for name in names:
        for item in fields:
            if str(item.get("name") or "").strip() == name:
                return str(item.get("value") or "").strip()
    return ""


def _flow_mark(fields: List[Dict[str, Any]]) -> str:
    return _field_value(fields, ("流程编号",))


class TravelSource:
    def __init__(self, policy: SourcePolicy, *, transfer=None, browser=None):
        self.policy = policy
        self.transfer = (
            transfer if transfer is not None else attachments.DownloadTransfer(policy)
        )
        self.browser = (
            browser if browser is not None else travel_browser.BrowserAdapter(policy)
        )

    def _trip_url(self, flow_mark: str) -> str:
        query = urllib.parse.urlencode(
            {"czlx": "D", "closed": "1", "bxdh": flow_mark, "isSsoLogin": "1"}
        )
        return f"{self.policy.travel_origin}/fcc/fcecf/bxdHome/index.html?{query}"

    def _oa_jump_url(self, fields: List[Dict[str, Any]]) -> str:
        for item in fields:
            if str(item.get("name") or "").strip() != "附件数量":
                continue
            for key in ("raw_value", "value"):
                match = HREF_RE.search(str(item.get(key) or ""))
                if match:
                    return urllib.parse.urljoin(
                        f"{self.policy.attachments.oa_origin}/",
                        html.unescape(match.group(1)),
                    )
        return ""

    def _read_candidates(
        self, urls, *, timeout_sec, flow_mark, expected_resource_count
    ):
        state = self.transfer.rpa_storage_state(timeout_sec=timeout_sec)
        if not state:
            return [CandidateObservation.failure("session", "session_unavailable")]
        results = self.browser.read_attempt(
            urls,
            timeout_sec=timeout_sec,
            storage_state=state,
            headless=True,
            expected_marker=flow_mark,
            expected_resource_count=expected_resource_count,
        )
        if any(
            item.assess(
                expected_marker=flow_mark,
                expected_resource_count=expected_resource_count,
            ).attempt_complete
            for item in results
        ):
            return results
        if attachments.browser_visible_fallback_enabled():
            return [
                *results,
                *self.browser.read_attempt(
                    urls,
                    timeout_sec=timeout_sec,
                    storage_state=state,
                    headless=False,
                    expected_marker=flow_mark,
                    expected_resource_count=expected_resource_count,
                ),
            ]
        return results

    def collect_evidence(
        self,
        cfg: dict[str, Any],
        data: dict[str, Any],
        request_id: str,
        *,
        allow_browser_download: bool,
        timeout_sec: int = 20,
    ) -> TravelEvidence:
        if str(cfg.get("flow_key") or "") != "travel_expense":
            return TravelEvidence.empty()
        fields = evidence_packet.extract_fields(data)
        if not allow_browser_download:
            return TravelEvidence.failure(
                DISCOVERY_FAILED,
                "本轮未授权读取商旅附件信息",
                diagnostics=[
                    {"stage": "authorization", "reason": "browser_read_not_authorized"}
                ],
            )
        flow_mark = _flow_mark(fields)
        if not flow_mark:
            return TravelEvidence.failure(
                DISCOVERY_FAILED,
                "缺少流程编号，无法读取商旅附件信息",
                diagnostics=[{"stage": "identity", "reason": "flow_marker_missing"}],
            )
        if runtime_control.stop_requested():
            return TravelEvidence.failure(
                runtime_control.STOP_CODE, "运行已请求停止，商旅附件信息未继续读取"
            )
        if not self.browser.ensure_available():
            return TravelEvidence.failure(
                DISCOVERY_FAILED,
                "商旅附件读取浏览器不可用",
                diagnostics=[{"stage": "browser", "reason": "browser_unavailable"}],
            )
        urls = list(
            dict.fromkeys(
                url
                for url in (self._oa_jump_url(fields), self._trip_url(flow_mark))
                if url
            )
        )
        match = re.search(r"\d+", _field_value(fields, ("附件数量",)))
        expected = 0 if match is not None and int(match.group()) == 0 else None
        try:
            observations = self._read_candidates(
                urls,
                timeout_sec=timeout_sec,
                flow_mark=flow_mark,
                expected_resource_count=expected,
            )
        except Exception as error:
            observations = [
                CandidateObservation.failure("browser", "browser_failed", error=error)
            ]
        return TravelEvidence.assemble(
            observations,
            fields=fields,
            request_id=request_id,
            flow_mark=flow_mark,
            expected_resource_count=expected,
        )
