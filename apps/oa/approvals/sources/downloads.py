from __future__ import annotations

from apps.oa.approvals.sources.download_errors import (
    Downloaded,
    DownloadError,
    Failed,
    Unavailable,
    TransferOutcome,
)
from apps.oa.approvals.source_policy import SourcePolicy

import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Tuple
import urllib.parse
import urllib.request

from apps.oa.materials.resources import (
    resource_name,
    safe_filename,
    deterministic_filename,
    _quote_download_url,
)
from apps.oa.runtime import files as material_store
from apps.oa.runtime import slots as resource_slots
from apps.oa.runtime import control as runtime_control
from apps.oa.approvals.sources import browser_session as rpa_session
from apps.oa.approvals.sources import http as safe_http
from apps.oa.approvals.sources.download_errors import (
    ATTACHMENT_ACCESS_DENIED_CODE,
    OA_SESSION_EXPIRED_CODE,
    detect_oa_download_fail_code,
)


DOWNLOADS = Path.home() / "Downloads"
YES_VALUES = {"1", "true", "yes", "y", "on", "YES"}
CHROME_LAUNCH_TIMEOUT_CODE = "attachment_download_launch_timeout"
DOWNLOAD_TIMEOUT_CODE = "attachment_download_timeout"
RESOURCE_SLOT_TIMEOUT_CODE = "attachment_download_capacity_timeout"
FATAL_DOWNLOAD_ERROR_CODES = {
    OA_SESSION_EXPIRED_CODE,
    CHROME_LAUNCH_TIMEOUT_CODE,
    DOWNLOAD_TIMEOUT_CODE,
    runtime_control.STOP_CODE,
    *rpa_session.FATAL_SESSION_CODES,
}
RPA_STORAGE_STATE_CACHE_SEC = 60.0
DEFAULT_GLOBAL_DOWNLOAD_CAPACITY = 6
MAX_GLOBAL_DOWNLOAD_CAPACITY = 8


def is_enabled(value: Any) -> bool:
    return (
        str(value or "").strip() in YES_VALUES
        or str(value or "").strip().lower() in YES_VALUES
    )


def _global_download_capacity() -> int:
    raw = str(os.environ.get("OA_ATTACHMENT_GLOBAL_DOWNLOAD_CAPACITY") or "").strip()
    try:
        configured = int(raw or DEFAULT_GLOBAL_DOWNLOAD_CAPACITY)
    except Exception:
        configured = DEFAULT_GLOBAL_DOWNLOAD_CAPACITY
    return min(max(1, configured), MAX_GLOBAL_DOWNLOAD_CAPACITY)


def _resource_slot_wait_timeout(timeout_sec: int) -> float:
    raw = str(os.environ.get("OA_RESOURCE_SLOT_WAIT_TIMEOUT_SEC") or "").strip()
    try:
        configured = float(raw) if raw else max(30.0, float(timeout_sec) * 2.0)
    except Exception:
        configured = max(30.0, float(timeout_sec) * 2.0)
    return max(0.1, configured)


def _looks_like_html(path: Path) -> bool:
    try:
        head = path.read_bytes()[:512].lstrip().lower()
    except Exception:
        return False
    return (
        head.startswith(b"<!doctype html")
        or head.startswith(b"<html")
        or b"<html" in head[:200]
    )


def _download_block_message(code: str, name: str) -> str:
    if code == ATTACHMENT_ACCESS_DENIED_CODE:
        return (
            f"{ATTACHMENT_ACCESS_DENIED_CODE}: {name}: 附件访问被拒绝，需人工核对权限"
        )
    if code == runtime_control.STOP_CODE:
        return f"{runtime_control.STOP_CODE}: {name}: 已请求停止财务预审，停止附件下载"
    if code == OA_SESSION_EXPIRED_CODE:
        return f"{OA_SESSION_EXPIRED_CODE}: {name}: OA 登录态失效，已停止附件下载"
    if code == CHROME_LAUNCH_TIMEOUT_CODE:
        return f"{CHROME_LAUNCH_TIMEOUT_CODE}: {name}: Chrome 启动超时，已停止附件下载"
    if code == DOWNLOAD_TIMEOUT_CODE:
        return f"{DOWNLOAD_TIMEOUT_CODE}: {name}: 下载超时"
    return f"{name}: 附件下载被阻断"


def _file_download_fail_code(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except Exception:
        return ""
    return detect_oa_download_fail_code(text)


def valid_attachment_file(path: Path) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size > 0
            and not path.name.endswith(".crdownload")
            and not _looks_like_html(path)
        )
    except Exception:
        return False


def close_chrome_blocking_tabs() -> None:
    if is_enabled(os.environ.get("OA_KEEP_BROWSER_TABS")):
        return
    close_chrome_download_tab("")


def chrome_launch_settle_sec() -> float:
    raw = str(os.environ.get("OA_CHROME_LAUNCH_SETTLE_SEC") or "").strip()
    if not raw:
        return 0.5
    try:
        return max(0.0, min(5.0, float(raw)))
    except Exception:
        return 0.5


def _read_process_output(proc: subprocess.Popen) -> str:
    try:
        stdout, stderr = proc.communicate(timeout=0.2)
    except Exception:
        stdout, stderr = "", ""
    return str(stderr or stdout or "").strip()[:300]


def _close_process_pipes(proc: subprocess.Popen) -> None:
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if stream:
                stream.close()
        except Exception:
            pass


def _browser_timeout_ms(timeout_sec: int | float) -> int:
    try:
        seconds = int(float(timeout_sec))
    except Exception:
        seconds = 5
    return max(1000, seconds * 1000)


def browser_silent_enabled() -> bool:
    value = str(os.environ.get("OA_BROWSER_SILENT") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def headless_browser_download_enabled() -> bool:
    return is_enabled(os.environ.get("OA_HEADLESS_BROWSER_DOWNLOAD"))


def browser_visible_fallback_enabled() -> bool:
    return is_enabled(os.environ.get("OA_BROWSER_VISIBLE_FALLBACK"))


def _unique_download_path(filename: str) -> Path:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename, "download.bin")
    path = DOWNLOADS / name
    if not path.exists():
        return path
    stem = path.stem or "download"
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = DOWNLOADS / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    return DOWNLOADS / f"{stem}-{int(time.time())}{suffix}"


def _headless_download_timeout_sec() -> int:
    raw = str(os.environ.get("OA_HEADLESS_DOWNLOAD_TIMEOUT_SEC") or "").strip()
    if not raw:
        return 20
    try:
        return max(1, min(120, int(raw)))
    except Exception:
        return 20


def _cookie_domain_matches(host: str, domain: str) -> bool:
    hostname = str(host or "").strip().lower()
    cookie_domain = str(domain or "").strip().lower().lstrip(".")
    return bool(
        hostname
        and cookie_domain
        and (hostname == cookie_domain or hostname.endswith(f".{cookie_domain}"))
    )


def close_chrome_download_tab(url: str) -> None:
    if is_enabled(os.environ.get("OA_KEEP_BROWSER_TABS")):
        return
    if not shutil.which("osascript"):
        return
    script = r"""
on run argv
  set targetUrl to item 1 of argv
  tell application "Google Chrome"
    repeat with chromeWindow in windows
      set tabCount to count of tabs of chromeWindow
      repeat with tabIndex from tabCount to 1 by -1
        set chromeTab to tab tabIndex of chromeWindow
        set tabUrl to URL of chromeTab
        set shouldClose to false
        if targetUrl is not "" and (tabUrl is targetUrl or tabUrl starts with targetUrl) then
          set shouldClose to true
        end if
        repeat with markerIndex from 2 to count of argv
          set markerText to item markerIndex of argv
          if markerText is not "" and tabUrl contains markerText then
            set shouldClose to true
          end if
        end repeat
        if shouldClose then
          close chromeTab
        end if
      end repeat
    end repeat
  end tell
end run
"""
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                script,
                url,
                "noright.jsp",
                "ddcodeTimeout",
                "ddcodetimeout",
                "login_required",
                "loginrequired",
                "login.jsp",
                "/login",
            ],
            text=True,
            encoding="utf-8",
            errors="ignore",
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        pass


class _BrowserCache:
    def __init__(self):
        self.policy = None
        self.storage_state = None
        self.error = None
        self.cookie_headers = {}
        self.lock = threading.RLock()

    def select(self, policy):
        if self.policy != policy:
            self.policy = policy
            self.storage_state = None
            self.error = None
            self.cookie_headers.clear()


_BROWSER_CACHE = _BrowserCache()


class DownloadTransfer:
    def __init__(self, policy: SourcePolicy, *, initialize_context=None):
        self.policy = policy
        self._initialize_context = initialize_context
        self._browser_cache = _BROWSER_CACHE

    def normalize_download_url(
        self, raw_url: str, *, allow_extra_hosts: bool = False
    ) -> str:
        return self.policy.attachments.download_url(
            raw_url, allow_extra=allow_extra_hosts
        )

    def chrome_tab_urls(self) -> List[str]:
        try:
            return rpa_session.list_tab_urls(self._cdp_endpoint(), timeout_sec=2)
        except Exception:
            return []

    def chrome_download_fail_code(self, *, close_blocking_tabs: bool = False) -> str:
        code = detect_oa_download_fail_code("\n".join(self.chrome_tab_urls()))
        if code and close_blocking_tabs:
            close_chrome_blocking_tabs()
        return code

    def chrome_remote_debugging_args(self) -> List[str]:
        return [
            f"--remote-debugging-address={urllib.parse.urlsplit(self.policy.browser.cdp_endpoint).hostname}",
            f"--remote-debugging-port={str(urllib.parse.urlsplit(self.policy.browser.cdp_endpoint).port)}",
        ]

    def _cdp_endpoint(self) -> str:
        return self.policy.browser.cdp_endpoint

    def rpa_storage_state(self, *, timeout_sec: int = 5) -> Dict[str, Any] | None:
        with self._browser_cache.lock:
            self._browser_cache.select(self.policy)
            now = time.monotonic()
            if (
                self._browser_cache.storage_state
                and self._browser_cache.storage_state[0] > now
            ):
                self._browser_cache.error = None
                return self._browser_cache.storage_state[1]
            oa_host = (
                urllib.parse.urlparse(self.policy.attachments.oa_origin).hostname or ""
            )
            try:
                state = rpa_session.capture_storage_state(
                    self._cdp_endpoint(),
                    allowed_hosts={oa_host, *self.policy.attachments.extra_hosts},
                    required_host=oa_host,
                    timeout_sec=timeout_sec,
                )
            except rpa_session.RpaSessionError as err:
                self._browser_cache.error = err
                return None
            self._browser_cache.error = None
            self._browser_cache.storage_state = (
                now + RPA_STORAGE_STATE_CACHE_SEC,
                state,
            )
            return state

    def rpa_storage_state_error_message(self) -> str:
        if not self._browser_cache.error:
            return ""
        return f"{self._browser_cache.error.code}: {self._browser_cache.error}"

    def rpa_web_session_status(self, *, timeout_sec: int = 5) -> Dict[str, str]:
        state = self.rpa_storage_state(timeout_sec=timeout_sec)
        if not state:
            code = (
                self._browser_cache.error.code
                if self._browser_cache.error is not None
                else rpa_session.RPA_SESSION_MISSING_CODE
            )
            raise RuntimeError(code)
        host = urllib.parse.urlparse(self.policy.attachments.oa_origin).hostname or ""
        try:
            page = rpa_session.probe_page_session(
                self._cdp_endpoint(),
                required_host=host,
                timeout_sec=timeout_sec,
            )
        except rpa_session.RpaSessionError as error:
            raise RuntimeError(error.code) from error
        if page["status"] == "expired":
            raise RuntimeError(OA_SESSION_EXPIRED_CODE)
        return {
            "status": "ok",
            "authentication": "ok",
            "source": "rpa_page",
        }

    def _capture_browser_download(
        self,
        url: str,
        name: str,
        *,
        dest_dir: Path | None,
        resource: dict,
        timeout_sec: int,
        headless: bool,
    ) -> Path:
        from playwright.sync_api import sync_playwright

        state = self.rpa_storage_state(timeout_sec=5)
        if not state:
            raise RuntimeError(
                self.rpa_storage_state_error_message()
                or f"{rpa_session.RPA_SESSION_MISSING_CODE}: RPA Chrome 未发现 OA 登录态"
            )
        with sync_playwright() as playwright:
            browser = None
            try:
                launch_kwargs = {"headless": headless}
                if Path(str(self.policy.browser.chrome_binary)).exists():
                    launch_kwargs["executable_path"] = str(
                        self.policy.browser.chrome_binary
                    )
                browser = playwright.chromium.launch(**launch_kwargs)
                context = browser.new_context(
                    accept_downloads=False, storage_state=state
                )
                page = context.new_page()
                with page.expect_download(
                    timeout=_browser_timeout_ms(timeout_sec)
                ) as event:
                    try:
                        page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=_browser_timeout_ms(timeout_sec),
                        )
                    except Exception as navigation_error:
                        if "net::ERR_ABORTED" not in str(
                            navigation_error
                        ) and "Page.goto: Download is starting" not in str(
                            navigation_error
                        ):
                            raise
                download = event.value
                captured_url = self.normalize_download_url(
                    download.url, allow_extra_hosts=True
                )
                host = urllib.parse.urlsplit(captured_url).hostname or ""
                cookie = "; ".join(
                    f"{item['name']}={item['value']}"
                    for item in context.cookies(captured_url)
                    if _cookie_domain_matches(host, item.get("domain", ""))
                )
                request = urllib.request.Request(
                    _quote_download_url(captured_url),
                    headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie},
                )
                suggested = download.suggested_filename or name
                target = (
                    dest_dir / deterministic_filename(resource, suggested)
                    if dest_dir is not None
                    else _unique_download_path(suggested)
                )
                with safe_http.urlopen(
                    request,
                    timeout=max(1, int(timeout_sec)),
                    allow_same_origin_redirects=True,
                ) as response:
                    material_store.copy_stream(
                        target,
                        response,
                        source={
                            "request_id": resource.get("_workflow_request_id"),
                            "resource_id": resource.get("id"),
                            "kind": "attachment",
                        },
                    )
                return target
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

    def _headless_chrome_download(
        self, url: str, name: str, *, headless: bool = True
    ) -> str:
        try:
            self._capture_browser_download(
                url,
                name,
                dest_dir=None,
                resource={},
                timeout_sec=_headless_download_timeout_sec(),
                headless=headless,
            )
            return ""
        except Exception as error:
            detail = str(error).strip()[:300]
            code = detect_oa_download_fail_code(detail)
            return (
                _download_block_message(code, name)
                if code
                else detail or "browser download failed"
            )

    def launch_chrome_download(self, url: str, name: str) -> str:
        if browser_silent_enabled() or headless_browser_download_enabled():
            error = self._headless_chrome_download(url, name)
            if not error:
                return ""
            if not browser_visible_fallback_enabled():
                return f"attachment_download_headless_failed: {name}: {error}"
        return self._headless_chrome_download(url, name, headless=False)

    def _direct_extra_host_download(
        self, resource: Dict[str, Any], url: str, *, dest_dir: Path, timeout_sec: int
    ) -> TransferOutcome:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname not in self.policy.attachments.extra_hosts:
            return Unavailable()
        name = resource_name(resource) or "attachment.bin"
        dest = dest_dir / deterministic_filename(resource, name)
        request = urllib.request.Request(
            _quote_download_url(url), headers={"User-Agent": "Mozilla/5.0"}
        )
        try:
            with (
                safe_http.urlopen(
                    request,
                    timeout=max(1, int(timeout_sec)),
                    allow_same_origin_redirects=True,
                ) as resp,
            ):
                material_store.copy_stream(
                    dest,
                    resp,
                    source={
                        "request_id": resource.get("_workflow_request_id"),
                        "resource_id": resource.get("id"),
                        "kind": "attachment",
                    },
                )
            return Downloaded(dest)
        except Exception as err:
            if isinstance(err, safe_http.HttpBoundaryError):
                raise
            if str(err).startswith("material_"):
                return Failed(
                    DownloadError(str(err), f"{err}: 附件空间不足或路径不可信")
                )
            return Failed(
                DownloadError(
                    "attachment_download_failed",
                    f"attachment_download_failed: {name}: 直连下载失败: {str(err).strip()[:300]}",
                )
            )

    def _rpa_cookie_header(self, host: str, *, timeout_sec: int = 2) -> str:
        with self._browser_cache.lock:
            self._browser_cache.select(self.policy)
            now = time.monotonic()
            cache_key = str(host or "").strip().lower()
            cached = self._browser_cache.cookie_headers.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]
            state = self.rpa_storage_state(timeout_sec=timeout_sec)
            cookies: List[str] = []
            for item in (state or {}).get("cookies") or []:
                if not isinstance(item, dict) or not _cookie_domain_matches(
                    host, str(item.get("domain") or "")
                ):
                    continue
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "")
                if name:
                    cookies.append(f"{name}={value}")
            header = "; ".join(cookies)
            if header and cache_key:
                self._browser_cache.cookie_headers[cache_key] = (
                    now + RPA_STORAGE_STATE_CACHE_SEC,
                    header,
                )
            return header

    def native(
        self, resource: Dict[str, Any], url: str, *, dest_dir: Path, timeout_sec: int
    ) -> TransferOutcome:
        parsed = urllib.parse.urlparse(url)
        oa_host_name = (
            urllib.parse.urlparse(self.policy.attachments.oa_origin).hostname or ""
        )
        if parsed.hostname not in {oa_host_name, *self.policy.attachments.extra_hosts}:
            return Unavailable()
        cookie_header = str(resource.get("_ephemeral_cookie_header") or "").strip()
        if not cookie_header:
            cookie_header = self._rpa_cookie_header(parsed.hostname or "")
        if not cookie_header:
            return Unavailable()
        name = resource_name(resource) or "attachment.bin"
        dest = dest_dir / deterministic_filename(resource, name)
        request = urllib.request.Request(
            _quote_download_url(url),
            headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie_header},
        )
        try:
            with safe_http.urlopen(
                request,
                timeout=max(1, int(timeout_sec)),
                allow_same_origin_redirects=True,
            ) as resp:
                response_code = detect_oa_download_fail_code(resp.geturl())
                if response_code:
                    return Failed(
                        DownloadError(
                            response_code, _download_block_message(response_code, name)
                        )
                    )
                material_store.copy_stream(
                    dest,
                    resp,
                    source={
                        "request_id": resource.get("_workflow_request_id"),
                        "resource_id": resource.get("id"),
                        "kind": "attachment",
                    },
                )
            if not valid_attachment_file(dest):
                code = _file_download_fail_code(dest)
                try:
                    dest.unlink()
                except Exception:
                    pass
                if code:
                    return Failed(
                        DownloadError(code, _download_block_message(code, name))
                    )
                return Unavailable()
            return Downloaded(dest)
        except Exception as error:
            if isinstance(error, safe_http.HttpBoundaryError):
                raise
            if str(error).startswith("material_"):
                return Failed(
                    DownloadError(str(error), f"{error}: 附件空间不足或路径不可信")
                )
            return Unavailable()

    def wait_new_download_or_block(
        self, before: set[str], expected_suffix: str, timeout_sec: int
    ) -> Tuple[Path | None, str]:
        deadline = time.time() + max(1, int(timeout_sec))
        next_block_check = 0.0
        while time.time() < deadline:
            if runtime_control.stop_requested():
                return None, runtime_control.STOP_CODE
            candidates = [
                item
                for item in DOWNLOADS.iterdir()
                if item.is_file()
                and str(item) not in before
                and not item.name.endswith(".crdownload")
            ]
            if candidates:
                preferred = [
                    item
                    for item in candidates
                    if not expected_suffix or item.suffix.lower() == expected_suffix
                ]
                files = preferred or candidates
                files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
                return files[0], ""
            now = time.time()
            if now >= next_block_check:
                code = self.chrome_download_fail_code(close_blocking_tabs=True)
                if code:
                    return None, code
                next_block_check = now + 1
            time.sleep(0.5)
        return None, self.chrome_download_fail_code(close_blocking_tabs=True)

    def _download_url(self, resource: Dict[str, Any]) -> str | Failed:
        raw_url = str(
            resource.get("downloadUrl") or resource.get("download_url") or ""
        ).strip()
        name = resource_name(resource) or "attachment.bin"
        if not raw_url:
            return Failed(
                DownloadError("attachment_download_failed", f"{name}: 缺少 downloadUrl")
            )
        try:
            return self.normalize_download_url(
                raw_url, allow_extra_hosts=bool(resource.get("allowExtraDownloadHost"))
            )
        except ValueError as error:
            return Failed(
                DownloadError(
                    "attachment_download_failed", f"{name}: 下载链接不合法: {error}"
                )
            )

    def _direct(
        self, resource: dict, *, dest_dir: Path, timeout_sec: int
    ) -> TransferOutcome:
        url = self._download_url(resource)
        if isinstance(url, Failed):
            return url
        dest_dir.mkdir(parents=True, exist_ok=True)
        extra = self._direct_extra_host_download(
            resource, url, dest_dir=dest_dir, timeout_sec=timeout_sec
        )
        if (
            isinstance(extra, Downloaded)
            or isinstance(extra, Failed)
            and extra.error.code.startswith("material_")
        ):
            return extra
        resource = dict(resource)
        host = urllib.parse.urlsplit(url).hostname or ""
        if host in {
            urllib.parse.urlsplit(self.policy.attachments.oa_origin).hostname,
            *self.policy.attachments.extra_hosts,
        }:
            resource["_ephemeral_cookie_header"] = resource.get(
                "_ephemeral_cookie_header"
            ) or self._rpa_cookie_header(host)
        outcome = self.native(resource, url, dest_dir=dest_dir, timeout_sec=timeout_sec)
        if (
            isinstance(outcome, Failed)
            and outcome.error.code == ATTACHMENT_ACCESS_DENIED_CODE
            and self._initialize_context is not None
        ):
            try:
                initialized = self._initialize_context(
                    resource, url, timeout_sec=timeout_sec
                )
            except rpa_session.RpaSessionError as error:
                return Failed(
                    DownloadError(
                        error.code, f"{error.code}: {resource_name(resource)}: {error}"
                    )
                )
            if initialized:
                if runtime_control.stop_requested():
                    return Failed(
                        DownloadError(
                            runtime_control.STOP_CODE,
                            _download_block_message(
                                runtime_control.STOP_CODE, resource_name(resource)
                            ),
                        )
                    )
                retried = self.native(
                    resource, url, dest_dir=dest_dir, timeout_sec=timeout_sec
                )
                return outcome if isinstance(retried, Unavailable) else retried
        return outcome

    def direct(
        self, resource: dict, *, dest_dir: Path, timeout_sec: int
    ) -> TransferOutcome:
        return direct_download(
            resource,
            timeout_sec=timeout_sec,
            operation=lambda: self._direct(
                resource, dest_dir=dest_dir, timeout_sec=timeout_sec
            ),
        )

    def browser(
        self, resource: dict, *, dest_dir: Path, timeout_sec: int
    ) -> TransferOutcome:
        name = resource_name(resource) or "attachment.bin"
        try:
            with resource_slots.acquire(
                "visible-browser",
                capacity=1,
                timeout_sec=_resource_slot_wait_timeout(timeout_sec),
            ):
                url = self._download_url(resource)
                if isinstance(url, Failed):
                    return url
                try:
                    path = self._capture_browser_download(
                        url,
                        name,
                        dest_dir=dest_dir,
                        resource=resource,
                        timeout_sec=timeout_sec,
                        headless=browser_silent_enabled()
                        or headless_browser_download_enabled(),
                    )
                    return Downloaded(path)
                except Exception as error:
                    code = (
                        str(error)
                        if str(error).startswith("material_")
                        else "attachment_download_failed"
                    )
                    return Failed(DownloadError(code, f"{code}: 浏览器会话下载失败"))
        except resource_slots.ResourceSlotTimeout:
            return Failed(
                DownloadError(
                    RESOURCE_SLOT_TIMEOUT_CODE,
                    f"{RESOURCE_SLOT_TIMEOUT_CODE}: {name}: 等待浏览器附件下载通道超时",
                )
            )


def direct_download(resource: dict, *, timeout_sec: int, operation) -> TransferOutcome:
    try:
        with resource_slots.acquire(
            "attachment-direct",
            capacity=_global_download_capacity(),
            timeout_sec=_resource_slot_wait_timeout(timeout_sec),
        ):
            return operation()
    except resource_slots.ResourceSlotTimeout:
        name = resource_name(resource) or "attachment.bin"
        return Failed(
            DownloadError(
                RESOURCE_SLOT_TIMEOUT_CODE,
                f"{RESOURCE_SLOT_TIMEOUT_CODE}: {name}: 等待全局附件下载通道超时",
            )
        )
