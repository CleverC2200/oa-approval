from __future__ import annotations
from contextlib import contextmanager
import html
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import signal
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List
from apps.oa.approvals.source_policy import SourcePolicy
from apps.oa.approvals.sources import downloads as attachments, travel_hotel
from apps.oa.approvals.sources.travel_evidence import (
    ATTACHMENT_ID_RE as TRIP_ATTACHMENT_ID_RE,
    CandidateObservation,
    attachment_id as _attachment_id_from_url,
    merge_inventory as _merge_attachment_inventory,
    page_pending as _trip_page_pending,
)
from apps.oa.runtime import slots as resource_slots

TRIP_ATTACHMENT_PATH_MARKERS = ("/opera/getFile/", "/opera/share/open/")
TRIP_ATTACHMENT_API_PATHS = {
    "/fcecf/fcc/pcapi/COMM_B2G_LoadBxdBaseInfoDetails",
    "/fcecf/fcc/pcapi/COMM_B2G_LoadBxmxList",
}
TRIP_EXPENSE_API_PATH = "/fcecf/fcc/pcapi/COMM_B2G_LoadBxmxList"
DEFAULT_TRAVEL_BROWSER_CAPACITY = 2
MAX_TRAVEL_BROWSER_CAPACITY = 8


def _travel_browser_capacity() -> int:
    raw = str(os.environ.get("OA_TRAVEL_BROWSER_CAPACITY") or "").strip()
    try:
        configured = int(raw or DEFAULT_TRAVEL_BROWSER_CAPACITY)
    except Exception:
        configured = DEFAULT_TRAVEL_BROWSER_CAPACITY
    return min(max(1, configured), MAX_TRAVEL_BROWSER_CAPACITY)


def _browser_slot_timeout(timeout_sec: int) -> float:
    return max(30.0, float(timeout_sec) * 2.0)


def _attachment_name(raw_name: str, url: str) -> str:
    value = html.unescape(str(raw_name or "")).strip()
    if value:
        parsed_value = urllib.parse.urlparse(value)
        if parsed_value.path and ("/" in value or "\\" in value):
            value = urllib.parse.unquote(Path(parsed_value.path).name)
    if not value:
        value = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    return attachments.safe_filename(value, "attachment.bin")


class _TripAttachmentDomParser(HTMLParser):
    def __init__(self, *, base_url: str, download_url) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.download_url = download_url
        self.candidates: List[Dict[str, str]] = []

    def _start(self, tag: str, raw_attrs: List[tuple[str, str | None]]) -> None:
        attrs = {str(key or "").lower(): str(value or "") for (key, value) in raw_attrs}
        if tag not in {"a", "img", "object", "embed"}:
            return
        raw_url = str(attrs.get("href") or attrs.get("src") or attrs.get("data") or "")
        url = self.download_url(raw_url, base_url=self.base_url)
        if not url:
            return
        raw_name = str(
            attrs.get("data-file-name")
            or attrs.get("data-name")
            or attrs.get("title")
            or attrs.get("alt")
            or attrs.get("download")
            or ""
        )
        self.candidates.append({"url": url, "name": _attachment_name(raw_name, url)})

    def handle_starttag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs)

    def handle_startendtag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs)


class _TravelReadDeadline(BaseException):
    pass


@contextmanager
def _wall_clock_timeout(timeout_sec: int):
    if threading.current_thread() is not threading.main_thread() or not hasattr(
        signal, "SIGALRM"
    ):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def _raise_timeout(_signum, _frame):
        raise _TravelReadDeadline("商旅页面读取超时")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, max(1, int(timeout_sec)))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _read_stable_body_text(page: Any, *, timeout_sec: int) -> str:
    deadline = time.monotonic() + max(1, timeout_sec)
    text = ""
    while True:
        text = page.locator("body").inner_text(
            timeout=max(1000, min(timeout_sec * 1000, 3000))
        )
        if text and (not _trip_page_pending(text)):
            return text
        if time.monotonic() >= deadline:
            return text
        page.wait_for_timeout(500)


def _activate_attachment_tab(root: Any, *, timeout_sec: float) -> bool:
    for lookup in (
        lambda: root.get_by_role("tab", name="附件信息", exact=True),
        lambda: root.get_by_text("附件信息", exact=True),
    ):
        try:
            locator = lookup()
            count = locator.count()
        except Exception:
            continue
        for index in range(count):
            try:
                target = locator.nth(index)
                if not target.is_visible():
                    continue
                target.click(timeout=max(1000, min(int(timeout_sec * 1000), 5000)))
                return True
            except Exception:
                continue
    return False


def _browser_roots(page: Any) -> List[Any]:
    roots = [page]
    try:
        frames = list(getattr(page, "frames", []) or [])
        main_frame = getattr(page, "main_frame", None)
    except Exception:
        return roots
    seen = {id(page)}
    for frame in frames:
        if frame is main_frame or id(frame) in seen:
            continue
        seen.add(id(frame))
        roots.append(frame)
    return roots


class _CandidateCapture:
    def __init__(self, policy):
        self.policy = policy
        self.resources = []
        self.inventory = []
        self.hotel_orders = []
        self.diagnostics = []

    def observe(self, response):
        self._capture_attachment_response(response)
        self._capture_hotel_order_response(response)

    def _trip_attachment_url(self, raw_url: str, *, base_url: str) -> str:
        value = str(raw_url or "").strip()
        if not value or value.startswith(("#", "javascript:", "data:", "blob:")):
            return ""
        url = urllib.parse.urljoin(base_url, html.unescape(value))
        parsed = urllib.parse.urlparse(url)
        trip_host = urllib.parse.urlparse(self.policy.travel_origin).hostname or ""
        if parsed.scheme != "https" or parsed.hostname != trip_host:
            return ""
        if not any(
            (
                marker.lower() in parsed.path.lower()
                for marker in TRIP_ATTACHMENT_PATH_MARKERS
            )
        ):
            return ""
        if not TRIP_ATTACHMENT_ID_RE.search(parsed.path):
            return ""
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
        )

    def _attachment_resources_from_dom(
        self, markup: str, *, base_url: str
    ) -> List[Dict[str, Any]]:
        parser = _TripAttachmentDomParser(
            base_url=base_url, download_url=self._trip_attachment_url
        )
        try:
            parser.feed(str(markup or ""))
        except Exception:
            return []
        resources: List[Dict[str, Any]] = []
        seen_urls: set[str] = set()
        for candidate in parser.candidates:
            url = str(candidate.get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            resources.append(
                {
                    "id": _attachment_id_from_url(url),
                    "name": str(candidate.get("name") or "attachment.bin"),
                    "downloadUrl": url,
                    "allowExtraDownloadHost": True,
                    "source": "travel_platform",
                }
            )
        return resources

    def _attachment_resources_from_api_payload(
        self, payload: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        inventory = []
        resources: List[Dict[str, Any]] = []
        seen_urls: set[str] = set()

        def add_attachment(item: Any, path: str, owner: str) -> None:
            if not isinstance(item, dict):
                inventory.append(
                    {
                        "stage": "attachment_inventory",
                        "reason": "record_invalid",
                        "source_path": path,
                    }
                )
                return
            raw_path = html.unescape(str(item.get("fjdz") or "")).strip()
            url = self._trip_attachment_url(
                raw_path, base_url=f"{self.policy.travel_origin}/"
            )
            if not url:
                relative_path = raw_path.lstrip("/")
                if relative_path.partition("/")[0].isdigit():
                    url = self._trip_attachment_url(
                        f"/fcfile/fcc/opera/getFile/{relative_path}",
                        base_url=f"{self.policy.travel_origin}/",
                    )
            placeholder = (
                all((key in item for key in ("fjdz", "fjmc", "fjlx")))
                and all(
                    (
                        item.get(key) in (None, "")
                        for key in ("fjdz", "fjmc", "fjlx", "wbfjdz", "fjwb")
                    )
                )
                and (item.get("fjdx") in (None, "", 0, "0"))
            )
            record = {
                "stage": "attachment_inventory",
                "source_path": path,
                "reason": "downloadable"
                if url
                else "empty_placeholder"
                if placeholder
                else "resource_address_invalid"
                if raw_path
                else "resource_address_missing",
            }
            for key, value in (
                ("attachment_ref", item.get("id")),
                ("owner_ref", owner),
            ):
                if isinstance(value, str) and re.fullmatch(
                    "[A-Za-z0-9_-]{1,80}", value
                ):
                    record[key] = value
            if url:
                record["file_ref"] = _attachment_id_from_url(url)
            inventory.append(record)
            if not url or url in seen_urls:
                return
            seen_urls.add(url)
            resources.append(
                {
                    "id": _attachment_id_from_url(url),
                    "name": _attachment_name(str(item.get("fjmc") or ""), url),
                    "downloadUrl": url,
                    "allowExtraDownloadHost": True,
                    "source": "travel_platform",
                }
            )

        def walk(value: Any, path: str = "payload", owner: str = "main") -> None:
            if isinstance(value, dict):
                owner = str(value.get("id") or owner)
                for key, child in value.items():
                    if not re.fullmatch("[A-Za-z][A-Za-z0-9_]{0,63}", str(key)):
                        continue
                    child_path = path + "." + str(key)
                    if str(key).lower() == "fjlist":
                        if isinstance(child, list):
                            for index, item in enumerate(child):
                                add_attachment(item, f"{child_path}[{index}]", owner)
                        else:
                            inventory.append(
                                {
                                    "stage": "attachment_inventory",
                                    "reason": "list_invalid",
                                    "source_path": child_path,
                                }
                            )
                    else:
                        walk(child, child_path, owner)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]", owner)

        walk(payload)
        return (resources, inventory)

    def _capture_attachment_response(self, response: Any) -> None:
        (resources, diagnostics, inventory) = (
            self.resources,
            self.diagnostics,
            self.inventory,
        )
        parsed = urllib.parse.urlparse(str(getattr(response, "url", "") or ""))
        trip_host = urllib.parse.urlparse(self.policy.travel_origin).hostname or ""
        if parsed.hostname != trip_host or parsed.path not in TRIP_ATTACHMENT_API_PATHS:
            return
        try:
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("success") is not True
                or (not isinstance(payload.get("result"), (dict, list)))
                or (not 200 <= getattr(response, "status", 200) < 300)
            ):
                inventory.append(
                    {"stage": "attachment_inventory", "reason": "response_invalid"}
                )
                return
            (discovered, observed) = self._attachment_resources_from_api_payload(
                payload
            )
            _merge_attachment_inventory(inventory, observed)
        except Exception as error:
            inventory.append(
                {"stage": "attachment_inventory", "reason": "response_invalid"}
            )
            diagnostics.append(
                {
                    "stage": "attachment_api",
                    "reason": "attachment_response_invalid",
                    "http_status": getattr(response, "status", 200),
                    "exception_type": type(error).__name__,
                }
            )
            return
        seen_urls = {str(item.get("downloadUrl") or "") for item in resources}
        for item in discovered:
            url = str(item.get("downloadUrl") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            resources.append(item)

    def _capture_hotel_order_response(self, response: Any) -> None:
        (orders, diagnostics) = (self.hotel_orders, self.diagnostics)
        parsed = urllib.parse.urlparse(str(getattr(response, "url", "") or ""))
        trip_host = urllib.parse.urlparse(self.policy.travel_origin).hostname or ""
        if parsed.hostname != trip_host or parsed.path != TRIP_EXPENSE_API_PATH:
            return
        try:
            payload = response.json()
            discovered = travel_hotel.orders_from_expenses(payload)
            if isinstance(payload, dict) and payload.get("success") is False:
                diagnostics.append(
                    {
                        "stage": "expense_api",
                        "reason": "expense_api_failed",
                        "http_status": getattr(response, "status", 200),
                        "api_success": False,
                    }
                )
        except Exception as error:
            diagnostics.append(
                {
                    "stage": "expense_api",
                    "reason": "expense_response_invalid",
                    "http_status": getattr(response, "status", 200),
                    "exception_type": type(error).__name__,
                }
            )
            return
        for item in discovered:
            if item not in orders:
                orders.append(item)


class _CandidateSession:
    def __init__(
        self,
        context,
        policy,
        url,
        *,
        deadline,
        budget,
        headless,
        expected_resource_count,
    ):
        (self.context, self.policy, self.url) = (context, policy, url)
        (self.deadline, self.budget) = (deadline, budget)
        (self.headless, self.expected_resource_count) = (
            headless,
            expected_resource_count,
        )
        self.page = context.new_page()
        self.capture = _CandidateCapture(policy)
        self.page.on("response", self.capture.observe)
        self.stage = "navigation"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.page.close()

    def read(self):
        try:
            response = self.page.goto(
                self.url,
                wait_until="domcontentloaded",
                timeout=max(1000, int(self.budget * 1000)),
            )
            status = getattr(response, "status", 200)
            if status >= 400:
                return CandidateObservation.failure(
                    "navigation",
                    "login_required" if status in {401, 403} else "page_http_failed",
                    headless=self.headless,
                    http_status=status,
                )
            self.page.wait_for_timeout(200)
            self.stage = "page"
            initial_text = _read_stable_body_text(
                self.page, timeout_sec=max(1, int(self.deadline - time.monotonic()))
            )
            details = travel_hotel.read_details(
                self.context.request,
                self.capture.hotel_orders,
                origin=self.policy.travel_origin,
                timeout_sec=max(0.5, self.deadline - time.monotonic()),
            )
            self.stage = "attachment_scan"
            snapshot = self._read_attachment_snapshot(
                timeout_sec=max(0.5, self.deadline - time.monotonic())
            )
            return self._observation(initial_text, details, snapshot)
        except Exception as error:
            return CandidateObservation.failure(
                self.stage, f"{self.stage}_failed", headless=self.headless, error=error
            )

    def _observation(self, initial_text, details, snapshot):
        text = str(snapshot.get("text") or initial_text)
        if initial_text and initial_text not in text:
            text = f"{initial_text}\n{text}".strip()
        observed = list(snapshot.get("resources") or [])
        resources = observed if snapshot.get("resource_scan_stable") else []
        if resources:
            try:
                cookie = self._ephemeral_trip_cookie_header(
                    self.context.cookies([self.policy.travel_origin])
                )
            except Exception:
                cookie = ""
            if cookie:
                for resource in resources:
                    resource["_ephemeral_cookie_header"] = cookie
        status = "resources_found" if resources else "read_failed"
        if (
            not resources
            and snapshot.get("attachment_tab_activated")
            and snapshot.get("empty_scan_stable")
        ):
            status = "empty"
        elif (
            not resources
            and (not snapshot.get("attachment_tab_activated"))
            and (not observed)
        ):
            status = "tab_unavailable"
        mode = "headless" if self.headless else "visible"
        return CandidateObservation(
            text,
            tuple(resources),
            status,
            tuple(self.capture.inventory),
            tuple(details),
            mode,
            tuple(({**item, "browser": mode} for item in self.capture.diagnostics)),
        )

    def _ephemeral_trip_cookie_header(self, cookies: List[Dict[str, Any]]) -> str:
        trip_host = urllib.parse.urlparse(self.policy.travel_origin).hostname or ""
        values: List[str] = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
            if not domain or not (
                trip_host == domain or trip_host.endswith(f".{domain}")
            ):
                continue
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            if name:
                values.append(f"{name}={value}")
        return "; ".join(values)

    def _browser_snapshot(self, page: Any, *, fallback_url: str) -> Dict[str, Any]:
        texts: List[str] = []
        resources: List[Dict[str, Any]] = []
        seen_texts: set[str] = set()
        seen_urls: set[str] = set()
        content_scan_failed = False
        for root in _browser_roots(page):
            try:
                text = str(root.locator("body").inner_text(timeout=1000) or "").strip()
            except Exception:
                text = ""
            if text and text not in seen_texts:
                seen_texts.add(text)
                texts.append(text)
            try:
                markup = root.content()
            except Exception:
                content_scan_failed = True
                continue
            base_url = str(
                getattr(root, "url", "") or getattr(page, "url", "") or fallback_url
            )
            for resource in self.capture._attachment_resources_from_dom(
                markup, base_url=base_url
            ):
                url = str(resource.get("downloadUrl") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                resources.append(resource)
        return {
            "text": "\n".join(texts),
            "resources": resources,
            "content_scan_failed": content_scan_failed,
        }

    def _read_attachment_snapshot(self, *, timeout_sec: float) -> Dict[str, Any]:
        (page, url) = (self.page, self.url)
        expected_resource_count = self.expected_resource_count
        response_resources = self.capture.resources
        snapshot: Dict[str, Any] = {"text": "", "resources": []}
        previous: tuple[tuple[str, str], ...] | None = None
        stable_rounds = 0
        stable_empty_rounds = 0
        activated_roots: set[int] = set()
        deadline = time.monotonic() + max(0.5, float(timeout_sec))
        while True:
            for root in _browser_roots(page):
                root_id = id(root)
                if root_id not in activated_roots and _activate_attachment_tab(
                    root, timeout_sec=timeout_sec
                ):
                    activated_roots.add(root_id)
            snapshot = self._browser_snapshot(page, fallback_url=url)
            seen_urls = {
                str(item.get("downloadUrl") or "")
                for item in snapshot.get("resources", [])
            }
            for item in response_resources or []:
                item_url = str(item.get("downloadUrl") or "")
                if not item_url or item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                snapshot.setdefault("resources", []).append(item)
            fingerprint = tuple(
                sorted(
                    (
                        (str(item.get("id") or ""), str(item.get("downloadUrl") or ""))
                        for item in snapshot.get("resources", [])
                    )
                )
            )
            if snapshot.get("content_scan_failed"):
                previous = None
                stable_rounds = 0
            elif fingerprint == previous:
                stable_rounds += 1
            else:
                previous = fingerprint
                stable_rounds = 1
            if fingerprint:
                stable_empty_rounds = 0
                count_complete = (
                    expected_resource_count is None
                    or len(fingerprint) >= expected_resource_count
                )
                if stable_rounds >= 3 and count_complete:
                    snapshot["resource_scan_stable"] = True
                    break
            else:
                stable_empty_rounds = (
                    stable_empty_rounds + 1
                    if not snapshot.get("content_scan_failed")
                    else 0
                )
                if (
                    expected_resource_count == 0
                    and stable_empty_rounds >= 3
                    and activated_roots
                ):
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            page.wait_for_timeout(max(1, min(500, int(remaining * 1000))))
        snapshot.setdefault("resource_scan_stable", False)
        snapshot["attachment_tab_activated"] = bool(activated_roots)
        snapshot["empty_scan_stable"] = stable_empty_rounds >= 3
        return snapshot


class _BrowserAttempt:
    def __init__(
        self,
        policy,
        urls,
        *,
        timeout_sec,
        storage_state,
        headless,
        expected_marker,
        expected_resource_count,
    ):
        (self.policy, self.urls) = (policy, urls)
        (self.timeout_sec, self.storage_state, self.headless) = (
            timeout_sec,
            storage_state,
            headless,
        )
        (self.expected_marker, self.expected_resource_count) = (
            expected_marker,
            expected_resource_count,
        )
        self.results = []

    def read(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as error:
            return [
                CandidateObservation.failure(
                    "browser",
                    "browser_unavailable",
                    headless=self.headless,
                    error=error,
                )
            ]
        try:
            with sync_playwright() as playwright:
                with _wall_clock_timeout(max(1, self.timeout_sec) + 5):
                    launch = {
                        "headless": self.headless,
                        "timeout": max(1000, self.timeout_sec * 1000),
                    }
                    if Path(str(self.policy.browser.chrome_binary)).exists():
                        launch["executable_path"] = str(
                            self.policy.browser.chrome_binary
                        )
                    browser = playwright.chromium.launch(**launch)
                    context = browser.new_context(storage_state=self.storage_state)
                    self._read_candidates(context)
        except (_TravelReadDeadline, Exception) as error:
            self.results.append(
                CandidateObservation.failure(
                    "browser",
                    "browser_deadline"
                    if isinstance(error, _TravelReadDeadline)
                    else "browser_failed",
                    headless=self.headless,
                    error=error,
                )
            )
        return self.results

    def _read_candidates(self, context):
        deadline = time.monotonic() + max(1, self.timeout_sec)
        for index, url in enumerate(self.urls):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.results.append(
                    CandidateObservation.failure(
                        "browser", "browser_deadline", headless=self.headless
                    )
                )
                break
            budget = max(1.0, remaining / max(1, len(self.urls) - index))
            with _CandidateSession(
                context,
                self.policy,
                url,
                deadline=min(deadline, time.monotonic() + budget),
                budget=budget,
                headless=self.headless,
                expected_resource_count=self.expected_resource_count,
            ) as candidate:
                observation = candidate.read()
                self.results.append(observation)
                if observation.assess(
                    expected_marker=self.expected_marker,
                    expected_resource_count=self.expected_resource_count,
                ).attempt_complete:
                    break


class BrowserAdapter:
    def __init__(self, policy: SourcePolicy):
        self.policy = policy

    def read_attempt(
        self,
        urls,
        *,
        timeout_sec,
        storage_state,
        headless,
        expected_marker,
        expected_resource_count,
    ):
        name = "travel-browser" if headless else "visible-browser"
        capacity = _travel_browser_capacity() if headless else 1
        try:
            with resource_slots.acquire(
                name, capacity=capacity, timeout_sec=_browser_slot_timeout(timeout_sec)
            ):
                return _BrowserAttempt(
                    self.policy,
                    urls,
                    timeout_sec=timeout_sec,
                    storage_state=storage_state,
                    headless=headless,
                    expected_marker=expected_marker,
                    expected_resource_count=expected_resource_count,
                ).read()
        except resource_slots.ResourceSlotTimeout as error:
            return [
                CandidateObservation.failure(
                    "browser", "browser_slot_timeout", headless=headless, error=error
                )
            ]

    def _cdp_endpoint(self) -> str:
        return self.policy.browser.cdp_endpoint

    def _cdp_available(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"{self._cdp_endpoint()}/json/version", timeout=2
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _wait_for_cdp(self, timeout_sec: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() <= deadline:
            if self._cdp_available():
                return True
            time.sleep(0.25)
        return self._cdp_available()

    def ensure_available(self) -> bool:
        if self._cdp_available():
            return True
        return self._wait_for_cdp()
