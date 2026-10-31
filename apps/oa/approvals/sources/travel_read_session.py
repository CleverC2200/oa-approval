from __future__ import annotations

import base64
from contextlib import ExitStack
import dataclasses
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from apps.oa.approvals.sources import auth, downloads
from apps.oa.approvals.sources.download_errors import Downloaded, DownloadError, Failed
from apps.oa.approvals.sources.travel_browser import (
    _TravelReadDeadline,
    _wall_clock_timeout,
    _travel_browser_capacity,
)
from apps.oa.approvals.sources.travel_evidence import (
    CandidateObservation,
    attachment_id,
)
from apps.oa.approvals.sources.travel_page_compat import (
    BASE_PATH,
    EXP_PATH,
    SUMMARY_PATH,
    HOTEL_PATHS,
    CompatibleCandidate,
    ReadContext,
    ReadFailure,
    source_data,
    verify_trip_identity,
)
from apps.oa.approvals.sources.travel_read_auth import (
    api_storage_state,
    verified_identity,
    ACCOUNT_PATH,
    ApiSessionError,
)
from apps.oa.approvals.sources.travel_read_transport import Policy, ReadonlyProxy
from apps.oa.runtime import control, slots


ACCEPTED_MOLI_SHA256 = frozenset(
    {"ac6e4bf3acd4ccc2fb0fefa305b32a935e93ab1a5302ed1983b7020d6bf65b25"}
)


class TravelReadSession:
    def __init__(self, policy):
        self.policy = policy
        self.stack = ExitStack()
        self.engine_stack = ExitStack()
        self.transport = None
        self.context = None
        self.page = None
        self.payloads = []
        self.attempts = []
        self.transport_failures = []
        self.files = frozenset()
        self.marker = ""
        self.owner = threading.get_ident()
        self.closed = False

    def ensure_available(self):
        return not self.closed

    def _remaining(self, deadline):
        if threading.get_ident() != self.owner:
            raise ReadFailure("travel_session_thread_mismatch")
        if control.stop_requested():
            raise ReadFailure(control.STOP_CODE)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReadFailure("travel_read_deadline")
        return remaining

    def _start_engine(self, engine, state, deadline, *, patches=True):
        from playwright.sync_api import sync_playwright

        self._remaining(deadline)
        self.transport = ReadonlyProxy(Policy(self.policy), patches=patches)
        proxy_url = self.transport.start()
        self.engine_stack.callback(self.transport.close)
        playwright = self.engine_stack.enter_context(sync_playwright())
        if engine == "moli":
            binary = self.policy.travel_read.moli_binary or shutil.which("moli")
            if not binary or not Path(binary).is_file():
                raise ReadFailure("travel_moli_binary_missing", retryable=True)
            if (
                hashlib.sha256(Path(binary).read_bytes()).hexdigest()
                not in ACCEPTED_MOLI_SHA256
            ):
                raise ReadFailure("travel_moli_version_unverified", retryable=True)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen(
                [
                    str(binary),
                    "serve",
                    "--layout",
                    "--resource",
                    "--port",
                    str(port),
                    "--http-proxy",
                    proxy_url,
                    "--http-no-proxy",
                    "",
                    "--ca-cert",
                    str(self.transport.cert),
                    "--log-level",
                    "error",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.engine_stack.callback(self._stop_process, process)
            endpoint = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            startup_deadline = min(deadline, time.monotonic() + 10)
            while True:
                if process.poll() is not None:
                    raise ReadFailure("travel_moli_start_failed", retryable=True)
                self._remaining(startup_deadline)
                try:
                    with opener.open(endpoint + "/json/version", timeout=0.2) as reply:
                        reply.read()
                    break
                except OSError:
                    time.sleep(0.05)
            browser = playwright.chromium.connect_over_cdp(
                endpoint, timeout=max(1, self._remaining(deadline) * 1000)
            )
        else:
            pub = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-in",
                    str(self.transport.cert),
                    "-pubkey",
                    "-noout",
                ],
                capture_output=True,
                check=True,
            ).stdout
            der = subprocess.run(
                ["openssl", "pkey", "-pubin", "-outform", "DER"],
                input=pub,
                capture_output=True,
                check=True,
            ).stdout
            pin = base64.b64encode(hashlib.sha256(der).digest()).decode()
            launch = {
                "headless": True,
                "proxy": {"server": proxy_url},
                "args": ["--ignore-certificate-errors-spki-list=" + pin],
                "timeout": max(1, self._remaining(deadline) * 1000),
            }
            if self.policy.browser.chrome_binary.is_file():
                launch["executable_path"] = str(self.policy.browser.chrome_binary)
            browser = playwright.chromium.launch(**launch)
        self.engine_stack.callback(browser.close)
        self.context = browser.new_context(
            storage_state=state, service_workers="block", accept_downloads=True
        )
        self.engine_stack.callback(self.context.close)
        self.context.set_default_timeout(max(1, self._remaining(deadline) * 1000))
        self.context.on("response", self._observe)
        self.engine = engine
        self.version = browser.version
        self._verify_oa(deadline)

    @staticmethod
    def _stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def _verify_oa(self, deadline):
        page = self.context.new_page()
        try:
            response = page.goto(
                self.policy.attachments.oa_origin + ACCOUNT_PATH,
                wait_until="domcontentloaded",
                timeout=max(1, self._remaining(deadline) * 1000),
            )
            if not response or response.status != 200:
                raise ReadFailure("travel_session_identity_unavailable")
            try:
                identity = json.loads(page.locator("body").inner_text())
            except (ValueError, TypeError):
                raise ReadFailure("travel_session_identity_unavailable") from None
            if not verified_identity(identity, auth.env("OA_UID")):
                raise ReadFailure("travel_session_identity_mismatch")
        finally:
            page.close()

    def _observe(self, response):
        parsed = urllib.parse.urlsplit(response.url)
        if parsed.hostname != self.transport.policy.trip_host or parsed.path not in {
            BASE_PATH,
            EXP_PATH,
            SUMMARY_PATH,
            *HOTEL_PATHS,
        }:
            return
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        self.payloads.append(
            {
                "path": parsed.path,
                "status": response.status,
                "payload": payload,
                "request_id": response.headers.get("x-oa-readonly-request-id"),
            }
        )

    def check_transport(self):
        if control.stop_requested():
            raise ReadFailure(control.STOP_CODE)
        if self.transport is None:
            return
        critical = {BASE_PATH, EXP_PATH, SUMMARY_PATH, *HOTEL_PATHS, ACCOUNT_PATH}
        for record in list(self.transport.records):
            path, decision = record.get("path", ""), record.get("decision")
            if decision == "forwarded" and record.get("status") in {401, 403}:
                raise ReadFailure("travel_upstream_access_denied")
            if decision == "compatibility_error":
                raise ReadFailure(
                    "travel_compatibility_version_mismatch", retryable=True
                )
            if path not in critical and "/fcfile/fcc/opera/" not in path:
                continue
            if decision in {
                "blocked",
                "blocked_authority",
                "blocked_body",
                "blocked_transport",
            }:
                raise ReadFailure("travel_required_request_blocked")
            if decision == "upstream_error" or (
                decision == "forwarded" and record.get("status", 200) >= 400
            ):
                raise ReadFailure("travel_upstream_read_failed")

    def _verify_delivery(self, request_ids):
        self.transport.wait_for_idle(timeout=2, quiet=0)
        self.check_transport()
        records = list(self.transport.records)
        for request_id in request_ids:
            if not request_id:
                raise ReadFailure("travel_delivery_unverified")
            events = [r for r in records if r.get("request_id") == request_id]
            if any(
                r.get("decision") in {"client_write_error", "client_response_error"}
                for r in events
            ):
                raise ReadFailure("travel_delivery_incomplete", retryable=True)
            if not any(
                r.get("decision") == "client_response_complete"
                and r.get("status") == 200
                for r in events
            ):
                raise ReadFailure("travel_delivery_incomplete", retryable=True)

    def _attempt(self, engine, state, urls, marker, expected, deadline, *, patches):
        try:
            self._start_engine(engine, state, deadline, patches=patches)
        except ReadFailure:
            raise
        except Exception as error:
            self.check_transport()
            raise ReadFailure("travel_engine_start_failed", retryable=True) from error
        if (
            not urls
            or urllib.parse.urlsplit(urls[0]).hostname != self.transport.policy.oa_host
        ):
            raise ReadFailure("travel_sso_jump_missing")
        context = ReadContext(self.context, self, marker, deadline)
        candidate = CompatibleCandidate(
            context,
            self.policy,
            urls[0],
            deadline=deadline,
            budget=self._remaining(deadline),
            headless=True,
            expected_resource_count=expected,
        )
        observation = candidate.read()
        self._remaining(deadline)
        self._verify_oa(deadline)
        self._verify_delivery([p["request_id"] for p in self.payloads])
        self.files = frozenset(str(r["id"]) for r in observation.download_targets)
        self.attempts.append(
            {
                "stage": "engine",
                "browser": engine,
                "engine": engine,
                "status": "complete",
                "version": self.version,
                "money_checks": len(context.money_checks),
                "identity_verified": True,
            }
        )
        return dataclasses.replace(
            observation,
            browser=engine,
            diagnostics=(
                *observation.diagnostics,
                *self.attempts,
                *self.transport_failures,
            ),
        )

    def read_attempt(
        self, urls, *, timeout_sec, expected_marker, expected_resource_count
    ):
        self.marker = expected_marker
        deadline = time.monotonic() + max(0.1, timeout_sec)
        state = None
        try:
            self.stack.enter_context(
                slots.acquire(
                    "travel-browser",
                    capacity=_travel_browser_capacity(),
                    timeout_sec=self._remaining(deadline),
                )
            )
            with _wall_clock_timeout(self._remaining(deadline)):
                state = (
                    api_storage_state(
                        self.policy, timeout_sec=self._remaining(deadline)
                    )
                    if self.policy.travel_read.session_source == "api"
                    else downloads.DownloadTransfer(self.policy).rpa_storage_state(
                        timeout_sec=self._remaining(deadline)
                    )
                )
            if not state:
                raise ReadFailure("travel_session_unavailable")
            engine = self.policy.travel_read.engine
            primary_deadline = time.monotonic() + self._remaining(deadline) * (
                0.6 if engine == "moli" else 1
            )
            try:
                with _wall_clock_timeout(self._remaining(primary_deadline)):
                    result = self._attempt(
                        engine,
                        state,
                        urls,
                        expected_marker,
                        expected_resource_count,
                        primary_deadline,
                        patches=True,
                    )
            except (_TravelReadDeadline, ReadFailure) as error:
                if isinstance(error, _TravelReadDeadline):
                    self.check_transport()
                    error = ReadFailure("travel_engine_deadline", retryable=True)
                self.attempts.append(
                    {
                        "stage": "engine",
                        "browser": engine,
                        "engine": engine,
                        "status": "failed",
                        "reason": error.code,
                    }
                )
                if engine != "moli" or not error.retryable:
                    raise error
                self._archive_transport(engine)
                self.engine_stack.close()
                self.engine_stack = ExitStack()
                self.page = self.context = None
                self.payloads.clear()
                with _wall_clock_timeout(self._remaining(deadline)):
                    result = self._attempt(
                        "chromium",
                        state,
                        urls,
                        expected_marker,
                        expected_resource_count,
                        deadline,
                        patches=error.code != "travel_compatibility_version_mismatch",
                    )
            return [result]
        except (_TravelReadDeadline, Exception) as error:
            reason = (
                error.code
                if isinstance(error, (ReadFailure, ApiSessionError))
                else "travel_read_deadline"
                if isinstance(error, _TravelReadDeadline)
                else "travel_session_failed"
            )
            failed = CandidateObservation.failure(
                "readonly_session",
                reason,
                headless=True,
                exception_type=type(error).__name__,
            )
            self._archive_transport(self.policy.travel_read.engine)
            return [
                dataclasses.replace(
                    failed,
                    diagnostics=(
                        *failed.diagnostics,
                        *self.attempts,
                        *self.transport_failures,
                    ),
                )
            ]
        finally:
            if state:
                state.clear()

    def owns(self, resource):
        return str(resource.get("source") or "") == "travel_platform" or str(
            resource.get("id") or ""
        ).startswith("travel_platform:")

    def _verify_trip(self):
        base, _, _ = source_data(self.payloads, self.marker)
        verify_trip_identity(self.page, base["bxdzb"])

    def _archive_transport(self, engine):
        for record in (
            list(self.transport.records) if self.transport is not None else []
        ):
            if record.get("decision") in {
                "upstream_error",
                "client_write_error",
                "client_response_error",
                "compatibility_error",
            }:
                self.transport_failures.append(
                    {
                        "stage": record.get("phase", "transport"),
                        "reason": record["decision"],
                        "browser": engine,
                        "request_id": record.get("request_id", ""),
                        **(
                            {"exception_type": record["error_type"]}
                            if record.get("error_type")
                            else {}
                        ),
                    }
                )

    def download(self, resource, *, dest_dir, timeout_sec):
        try:
            deadline = time.monotonic() + max(0.1, timeout_sec)
            self._remaining(deadline)
            url = self.policy.attachments.download_url(
                str(resource.get("downloadUrl") or resource.get("url") or ""),
                allow_extra=True,
            )

            raw_id = str(resource.get("id") or "").removeprefix("travel_platform:")
            parsed = urllib.parse.urlsplit(url)
            if (
                not self.page
                or raw_id not in self.files
                or attachment_id(url) != raw_id
                or parsed.hostname != self.transport.policy.trip_host
                or not self.transport.policy.allows("GET", parsed.hostname, parsed.path)
            ):
                raise ReadFailure("travel_download_binding_mismatch")
            self._verify_oa(deadline)
            self._verify_trip()
            result = self.page.evaluate(
                """async p=>{
                const a=new AbortController();const timer=setTimeout(()=>a.abort(),p.timeout);
                try{const r=await fetch(p.url,{credentials:'include',redirect:'manual',signal:a.signal});
                    const b=new Uint8Array(await r.arrayBuffer());if(b.length>25000000)throw Error('file_limit');
                    let s='';for(let i=0;i<b.length;i+=16384)s+=String.fromCharCode(...b.subarray(i,i+16384));
                    return {status:r.status,type:r.headers.get('content-type'),request_id:r.headers.get('X-OA-Readonly-Request-Id'),data:btoa(s)};
                }finally{clearTimeout(timer)}
            }""",
                {"url": url, "timeout": max(1, self._remaining(deadline) * 1000)},
            )
            self._verify_delivery([result["request_id"]])
            kind = str(result.get("type") or "").lower()
            body = base64.b64decode(result["data"], validate=True)
            if (
                result["status"] != 200
                or not body
                or any(
                    t in kind for t in ("text/", "application/json", "application/xml")
                )
            ):
                raise ReadFailure("travel_download_response_invalid")
            self._verify_oa(deadline)
            self._verify_trip()
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / downloads.deterministic_filename(resource)
            path.write_bytes(body)
            path.chmod(0o600)
            return Downloaded(path)
        except Exception as error:
            code = (
                error.code
                if isinstance(error, ReadFailure)
                else "travel_download_failed"
            )
            return Failed(DownloadError(code, code))

    def close(self):
        try:
            self.engine_stack.close()
        finally:
            self.stack.close()
            self.context = self.page = None
            self.payloads.clear()
            self.files = frozenset()
            self.marker = ""
            self.closed = True
