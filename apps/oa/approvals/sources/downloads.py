from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Tuple
import urllib.parse
import urllib.request

from apps.oa.materials import resources
from apps.oa.materials.resources import resource_name, safe_filename, deterministic_filename, _quote_download_url
from apps.oa.runtime import files as material_store
from apps.oa.runtime import slots as resource_slots
from apps.oa.runtime import control as runtime_control
from apps.oa.runtime import environment
from apps.oa.approvals.sources import browser_session as rpa_session
from apps.oa.approvals.sources import http as safe_http
from apps.oa.approvals.sources.download_errors import ATTACHMENT_ACCESS_DENIED_CODE, OA_SESSION_EXPIRED_CODE, detect_oa_download_fail_code


environment.load_local_env()

OA_HOST = os.environ.get("OA_BASE_URL", "").rstrip("/")
EXTRA_DOWNLOAD_HOSTS = {
    host.strip().lower()
    for host in os.environ.get("OA_EXTRA_DOWNLOAD_HOSTS", "").split(",")
    if host.strip()
}
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_USER_DATA_DIR = Path(
    os.environ.get(
        "OA_CHROME_USER_DATA_DIR",
        str(Path.home() / "Library/Application Support/OA RPA Chrome"),
    )
).expanduser()
RPA_PROFILE_DIRECTORY = os.environ.get("OA_CHROME_PROFILE_DIRECTORY", "Default")
CHROME_REMOTE_DEBUGGING_ADDRESS = os.environ.get(
    "OA_CHROME_REMOTE_DEBUGGING_ADDRESS", "127.0.0.1"
)
CHROME_REMOTE_DEBUGGING_PORT = os.environ.get("OA_CHROME_REMOTE_DEBUGGING_PORT", "9223")
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
_RPA_STORAGE_STATE_CACHE: Tuple[float, Dict[str, Any]] | None = None
_RPA_STORAGE_STATE_ERROR: rpa_session.RpaSessionError | None = None
_RPA_COOKIE_HEADER_CACHE: Dict[str, Tuple[float, str]] = {}
_RPA_CACHE_LOCK = threading.RLock()
DEFAULT_GLOBAL_DOWNLOAD_CAPACITY = 6
MAX_GLOBAL_DOWNLOAD_CAPACITY = 8


def is_enabled(value: Any) -> bool:
    return (
        str(value or "").strip() in YES_VALUES
        or str(value or "").strip().lower() in YES_VALUES
    )


def _global_download_capacity() -> int:
    raw = str(
        os.environ.get("OA_ATTACHMENT_GLOBAL_DOWNLOAD_CAPACITY") or ""
    ).strip()
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


def normalize_download_url(
    raw_url: str, *, oa_host: str | None = None, allow_extra_hosts: bool = False
) -> str:
    origin = OA_HOST if oa_host is None else str(oa_host).rstrip("/")
    allowed_hosts = {host for host in (urllib.parse.urlparse(origin).hostname,) if host}
    if allow_extra_hosts:
        allowed_hosts.update(EXTRA_DOWNLOAD_HOSTS)
    return resources.normalize_download_url(raw_url, oa_host=origin, allowed_hosts=allowed_hosts)


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


def download_error_code(message: str) -> str:
    text = str(message or "").strip()
    for code in ("resource_ownership_untrusted", "attachment_content_incomplete", "material_budget_exceeded", "material_path_untrusted"):
        if text.startswith(f"{code}:"):
            return code
    if text.startswith(f"{ATTACHMENT_ACCESS_DENIED_CODE}:"):
        return ATTACHMENT_ACCESS_DENIED_CODE
    for code in rpa_session.FATAL_SESSION_CODES:
        if text.startswith(f"{code}:"):
            return code
    if text.startswith(f"{runtime_control.STOP_CODE}:"):
        return runtime_control.STOP_CODE
    if text.startswith(f"{OA_SESSION_EXPIRED_CODE}:"):
        return OA_SESSION_EXPIRED_CODE
    if text.startswith(f"{CHROME_LAUNCH_TIMEOUT_CODE}:"):
        return CHROME_LAUNCH_TIMEOUT_CODE
    if text.startswith(f"{DOWNLOAD_TIMEOUT_CODE}:"):
        return DOWNLOAD_TIMEOUT_CODE
    if text.startswith(f"{RESOURCE_SLOT_TIMEOUT_CODE}:"):
        return RESOURCE_SLOT_TIMEOUT_CODE
    if "下载超时" in text:
        return DOWNLOAD_TIMEOUT_CODE
    return detect_oa_download_fail_code(text) or "attachment_download_failed"


def _download_block_message(code: str, name: str) -> str:
    if code == ATTACHMENT_ACCESS_DENIED_CODE:
        return f"{ATTACHMENT_ACCESS_DENIED_CODE}: {name}: 附件访问被拒绝，需人工核对权限"
    if code == runtime_control.STOP_CODE:
        return f"{runtime_control.STOP_CODE}: {name}: 已请求停止财务预审，停止附件下载"
    if code == OA_SESSION_EXPIRED_CODE:
        return (
            f"{OA_SESSION_EXPIRED_CODE}: {name}: OA 登录态失效，已停止附件下载"
        )
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


def chrome_tab_urls() -> List[str]:
    try:
        return rpa_session.list_tab_urls(_cdp_endpoint(), timeout_sec=2)
    except Exception:
        return []


def close_chrome_blocking_tabs() -> None:
    if is_enabled(os.environ.get("OA_KEEP_BROWSER_TABS")):
        return
    close_chrome_download_tab("")


def chrome_download_fail_code(*, close_blocking_tabs: bool = False) -> str:
    code = detect_oa_download_fail_code("\n".join(chrome_tab_urls()))
    if code and close_blocking_tabs:
        close_chrome_blocking_tabs()
    return code


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


def chrome_remote_debugging_args() -> List[str]:
    return [
        f"--remote-debugging-address={CHROME_REMOTE_DEBUGGING_ADDRESS}",
        f"--remote-debugging-port={CHROME_REMOTE_DEBUGGING_PORT}",
    ]


def _cdp_endpoint() -> str:
    return f"http://{CHROME_REMOTE_DEBUGGING_ADDRESS}:{CHROME_REMOTE_DEBUGGING_PORT}"


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


def rpa_storage_state(*, timeout_sec: int = 5) -> Dict[str, Any] | None:
    global _RPA_STORAGE_STATE_CACHE, _RPA_STORAGE_STATE_ERROR
    with _RPA_CACHE_LOCK:
        now = time.monotonic()
        if _RPA_STORAGE_STATE_CACHE and _RPA_STORAGE_STATE_CACHE[0] > now:
            _RPA_STORAGE_STATE_ERROR = None
            return _RPA_STORAGE_STATE_CACHE[1]
        oa_host = urllib.parse.urlparse(OA_HOST).hostname or ""
        try:
            state = rpa_session.capture_storage_state(
                _cdp_endpoint(),
                allowed_hosts={oa_host, *EXTRA_DOWNLOAD_HOSTS},
                required_host=oa_host,
                timeout_sec=timeout_sec,
            )
        except rpa_session.RpaSessionError as err:
            _RPA_STORAGE_STATE_ERROR = err
            return None
        _RPA_STORAGE_STATE_ERROR = None
        _RPA_STORAGE_STATE_CACHE = (now + RPA_STORAGE_STATE_CACHE_SEC, state)
        return state


def rpa_storage_state_error_message() -> str:
    if not _RPA_STORAGE_STATE_ERROR:
        return ""
    return f"{_RPA_STORAGE_STATE_ERROR.code}: {_RPA_STORAGE_STATE_ERROR}"


def rpa_web_session_status(*, timeout_sec: int = 5) -> Dict[str, str]:
    state = rpa_storage_state(timeout_sec=timeout_sec)
    if not state:
        code = (
            _RPA_STORAGE_STATE_ERROR.code
            if _RPA_STORAGE_STATE_ERROR is not None
            else rpa_session.RPA_SESSION_MISSING_CODE
        )
        raise RuntimeError(code)
    host = urllib.parse.urlparse(OA_HOST).hostname or ""
    try:
        page = rpa_session.probe_page_session(
            _cdp_endpoint(),
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


def _capture_browser_download(url: str, name: str, *, dest_dir: Path | None,
                              resource: dict, timeout_sec: int, headless: bool) -> Path:
    from playwright.sync_api import sync_playwright

    state = rpa_storage_state(timeout_sec=5)
    if not state:
        raise RuntimeError(rpa_storage_state_error_message() or
                           f"{rpa_session.RPA_SESSION_MISSING_CODE}: RPA Chrome 未发现 OA 登录态")
    with sync_playwright() as playwright:
        browser = None
        try:
            launch_kwargs = {'headless': headless}
            if Path(CHROME_BIN).exists():
                launch_kwargs['executable_path'] = CHROME_BIN
            browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context(accept_downloads=False, storage_state=state)
            page = context.new_page()
            with page.expect_download(timeout=_browser_timeout_ms(timeout_sec)) as event:
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=_browser_timeout_ms(timeout_sec))
                except Exception as navigation_error:
                    if ('net::ERR_ABORTED' not in str(navigation_error)
                            and 'Page.goto: Download is starting' not in str(navigation_error)):
                        raise
            download = event.value
            captured_url = normalize_download_url(download.url, allow_extra_hosts=True)
            host = urllib.parse.urlsplit(captured_url).hostname or ''
            cookie = '; '.join(f"{item['name']}={item['value']}" for item in context.cookies(captured_url)
                               if _cookie_domain_matches(host, item.get('domain', '')))
            request = urllib.request.Request(_quote_download_url(captured_url),
                headers={'User-Agent': 'Mozilla/5.0', 'Cookie': cookie})
            suggested = download.suggested_filename or name
            target = (dest_dir / deterministic_filename(resource, suggested)
                      if dest_dir is not None else _unique_download_path(suggested))
            with safe_http.urlopen(request, timeout=max(1, int(timeout_sec)),
                                   allow_same_origin_redirects=True) as response:
                material_store.copy_stream(target, response, source={
                    'request_id': resource.get('_workflow_request_id'),
                    'resource_id': resource.get('id'), 'kind': 'attachment',
                })
            return target
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def _headless_chrome_download(url: str, name: str, *, headless: bool = True) -> str:
    try:
        _capture_browser_download(url, name, dest_dir=None, resource={},
                                  timeout_sec=_headless_download_timeout_sec(), headless=headless)
        return ''
    except Exception as error:
        detail = str(error).strip()[:300]
        code = detect_oa_download_fail_code(detail)
        return _download_block_message(code, name) if code else detail or 'browser download failed'


def launch_chrome_download(url: str, name: str) -> str:
    if browser_silent_enabled() or headless_browser_download_enabled():
        error = _headless_chrome_download(url, name)
        if not error:
            return ''
        if not browser_visible_fallback_enabled():
            return f'attachment_download_headless_failed: {name}: {error}'
    return _headless_chrome_download(url, name, headless=False)


def _direct_extra_host_download(
    resource: Dict[str, Any], url: str, *, dest_dir: Path, timeout_sec: int
) -> Tuple[Path | None, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in EXTRA_DOWNLOAD_HOSTS:
        return None, ""
    name = resource_name(resource) or "attachment.bin"
    dest = dest_dir / deterministic_filename(resource, name)
    request = urllib.request.Request(
        _quote_download_url(url), headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with (
            safe_http.urlopen(request, timeout=max(1, int(timeout_sec)),
                              allow_same_origin_redirects=True) as resp,
        ):
            material_store.copy_stream(dest, resp, source={
                'request_id': resource.get('_workflow_request_id'),
                'resource_id': resource.get('id'), 'kind': 'attachment',
            })
        return dest, ""
    except Exception as err:
        if isinstance(err, safe_http.HttpBoundaryError):
            raise
        if str(err).startswith('material_'):
            return None, f'{err}: 附件空间不足或路径不可信'
        return (
            None,
            f"attachment_download_failed: {name}: 直连下载失败: {str(err).strip()[:300]}",
        )


def _cookie_domain_matches(host: str, domain: str) -> bool:
    hostname = str(host or "").strip().lower()
    cookie_domain = str(domain or "").strip().lower().lstrip(".")
    return bool(
        hostname
        and cookie_domain
        and (hostname == cookie_domain or hostname.endswith(f".{cookie_domain}"))
    )


def _rpa_cookie_header(host: str, *, timeout_sec: int = 2) -> str:
    with _RPA_CACHE_LOCK:
        now = time.monotonic()
        cache_key = str(host or "").strip().lower()
        cached = _RPA_COOKIE_HEADER_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        state = rpa_storage_state(timeout_sec=timeout_sec)
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
            _RPA_COOKIE_HEADER_CACHE[cache_key] = (
                now + RPA_STORAGE_STATE_CACHE_SEC,
                header,
            )
        return header


def _direct_session_cookie_download(
    resource: Dict[str, Any], url: str, *, dest_dir: Path, timeout_sec: int
) -> Tuple[Path | None, str]:
    parsed = urllib.parse.urlparse(url)
    oa_host_name = urllib.parse.urlparse(OA_HOST).hostname or ""
    if parsed.hostname not in {oa_host_name, *EXTRA_DOWNLOAD_HOSTS}:
        return None, ""
    cookie_header = str(resource.get("_ephemeral_cookie_header") or "").strip()
    if not cookie_header:
        cookie_header = _rpa_cookie_header(parsed.hostname or "")
    if not cookie_header:
        return None, ""
    name = resource_name(resource) or "attachment.bin"
    dest = dest_dir / deterministic_filename(resource, name)
    request = urllib.request.Request(
        _quote_download_url(url),
        headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie_header},
    )
    try:
        with safe_http.urlopen(
            request, timeout=max(1, int(timeout_sec)), allow_same_origin_redirects=True
        ) as resp:
            response_code = detect_oa_download_fail_code(resp.geturl())
            if response_code:
                return None, _download_block_message(response_code, name)
            material_store.copy_stream(dest, resp, source={
                'request_id': resource.get('_workflow_request_id'),
                'resource_id': resource.get('id'), 'kind': 'attachment',
            })
        if not valid_attachment_file(dest):
            code = _file_download_fail_code(dest)
            try:
                dest.unlink()
            except Exception:
                pass
            if code:
                return None, _download_block_message(code, name)
            return None, ""
        return dest, ""
    except Exception as error:
        if isinstance(error, safe_http.HttpBoundaryError):
            raise
        if str(error).startswith('material_'):
            return None, f'{error}: 附件空间不足或路径不可信'
        return None, ""


def wait_new_download_or_block(
    before: set[str], expected_suffix: str, timeout_sec: int
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
            code = chrome_download_fail_code(close_blocking_tabs=True)
            if code:
                return None, code
            next_block_check = now + 1
        time.sleep(0.5)
    return None, chrome_download_fail_code(close_blocking_tabs=True)


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


def _normalized_download_url(resource: Dict[str, Any]) -> Tuple[str, str]:
    raw_url = str(
        resource.get("downloadUrl") or resource.get("download_url") or ""
    ).strip()
    name = resource_name(resource) or "attachment.bin"
    if not raw_url:
        return "", f"{name}: 缺少 downloadUrl"
    try:
        return (
            normalize_download_url(
                raw_url,
                allow_extra_hosts=bool(resource.get("allowExtraDownloadHost")),
            ),
            "",
        )
    except ValueError as err:
        return "", f"{name}: 下载链接不合法: {err}"


def _download_resource_direct_uncoordinated(
    resource: Dict[str, Any], *, dest_dir: Path, timeout_sec: int, initialize_context=None,
) -> Tuple[Path | None, str]:
    url, error = _normalized_download_url(resource)
    if error:
        return None, error
    dest_dir.mkdir(parents=True, exist_ok=True)
    direct_path, _direct_err = _direct_extra_host_download(
        resource, url, dest_dir=dest_dir, timeout_sec=timeout_sec
    )
    if direct_path is not None:
        return direct_path, ""
    if _direct_err.startswith('material_'):
        return None, _direct_err
    resource = dict(resource)
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in {urllib.parse.urlsplit(OA_HOST).hostname, *EXTRA_DOWNLOAD_HOSTS}:
        resource["_ephemeral_cookie_header"] = (
            resource.get("_ephemeral_cookie_header") or _rpa_cookie_header(host)
        )
    direct_path, direct_err = _direct_session_cookie_download(
        resource, url, dest_dir=dest_dir, timeout_sec=timeout_sec
    )
    if (direct_path is None and download_error_code(direct_err) == ATTACHMENT_ACCESS_DENIED_CODE
            and initialize_context is not None):
        try:
            initialized = initialize_context(
                resource, url, timeout_sec=timeout_sec)
        except rpa_session.RpaSessionError as error:
            return None, f"{error.code}: {resource_name(resource)}: {error}"
        if initialized:
            if runtime_control.stop_requested():
                return None, _download_block_message(runtime_control.STOP_CODE, resource_name(resource))
            retry_path, retry_error = _direct_session_cookie_download(
                resource, url, dest_dir=dest_dir, timeout_sec=timeout_sec
            )
            return retry_path, retry_error or (direct_err if retry_path is None else "")
    if direct_path is not None or direct_err:
        return direct_path, direct_err
    return None, ""


def _download_resource_with_browser_uncoordinated(
    resource: Dict[str, Any], *, dest_dir: Path, timeout_sec: int
) -> Tuple[Path | None, str]:
    url, error = _normalized_download_url(resource)
    if error:
        return None, error
    name = resource_name(resource) or 'attachment.bin'
    try:
        return _capture_browser_download(url, name, dest_dir=dest_dir, resource=resource,
            timeout_sec=timeout_sec, headless=browser_silent_enabled() or headless_browser_download_enabled()), ''
    except Exception as browser_error:
        code = str(browser_error) if str(browser_error).startswith('material_') else 'attachment_download_failed'
        return None, f'{code}: 浏览器会话下载失败'


def _download_resource_with_browser(
    resource: Dict[str, Any], *, dest_dir: Path, timeout_sec: int
) -> Tuple[Path | None, str]:
    name = resource_name(resource) or "attachment.bin"
    try:
        with resource_slots.acquire(
            "visible-browser",
            capacity=1,
            timeout_sec=_resource_slot_wait_timeout(timeout_sec),
        ):
            return _download_resource_with_browser_uncoordinated(
                resource,
                dest_dir=dest_dir,
                timeout_sec=timeout_sec,
            )
    except resource_slots.ResourceSlotTimeout:
        return None, (
            f"{RESOURCE_SLOT_TIMEOUT_CODE}: {name}: 等待浏览器附件下载通道超时"
        )

