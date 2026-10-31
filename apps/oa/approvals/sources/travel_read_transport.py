from __future__ import annotations

import http.server
import json
import re
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from apps.oa.approvals.sources import http as safe_http
from apps.oa.approvals.sources.travel_page_patch import patch_response

OA_READ = frozenset(
    {
        "/api/encryptedTrans/getUnencryptedUrlList",
        "/api/system/language/base/getTransStat",
        "/api/doc/console/wmsystemsetting/getwmsetting",
        "/api/encryptedTrans/isEnableEncryptedTrans",
        "/api/portal/themeCenter/getMyTheme",
        "/api/security/isEnableRandomCode",
        "/api/integration/common1/checkCasIsDeployed",
        "/api/ec/dev/app/checkJoin",
        "/api/hrm/login/getAccountList",
        "/api/system/info/getOSinfo",
        "/api/hrm/login/getLoginForm",
        "/api/portal/plugin/getPlugins",
        "/api/hrm/login/getTimeOutSwitch",
        "/api/portal/menu/getFrontEndMenu",
        "/api/system/language/base/getActiveLanguage",
        "/api/portal/themeConfig/getThemeConfig",
    }
)
TRIP_READ = frozenset(
    {
        "/fcecf/fcc/pcapi/COMM_B2G_bldDetail",
        "/fccost/fcc/pcapi/COMM_B2G_QueryCommonConfigBusiness",
        "/fcecf/fcc/pcapi/COMM_B2G_QueryHotelSubsidy",
        "/fccost/fcc/pcapi/COMM_B2G_QueryFkfkdysList",
        "/fcecf/fcc/pcapi/COMM_B2G_LoadBxmxList",
        "/fcecf/fcc/pcapi/COMM_B2G_QueryBxdCheckResult",
        "/fcecf/fcc/pcapi/COMM_B2G_GetDefaultPaymentWay",
        "/fccost/fcc/pcapi/COMM_B2G_GetCspz",
        "/fccost/fcc/pcapi/COMM_B2G_GetFkFjmb",
        "/fccost/fcc/basicdata/getJcsjByLb",
        "/fcecf/fcc/pcapi/COMM_B2G_QueryBudgetRecord",
        "/fcecf/fcc/pcapi/COMM_B2G_LoadBxdJefo",
        "/fccost/fcc/fapiaosz/getPjyqcs",
        "/fcecf/fcc/pcapi/COMM_B2G_LoadBxdBaseInfoDetails",
        "/base/fcc/fcccorporatecompany/getLogo",
        "/fcc/connect/COMM_B2G_getLang",
        "/fcc/connect/COMM_B2G_getErrorCode",
        "/fcecf/fcc/pcapi/COMM_B2G_BxdPzsj",
        "/fcc/bxd/checkPageUrl",
        "/base/fcc/pcapi/COMM_B2G_InvoiceRep",
        "/fcecf/fcc/pcapi/COMM_B2G_QueryFyftByBxpch",
        "/fccost/fcc/pcapi/COMM_B2G_FkZdsxsAndZsjsfkq",
        "/fccost/fcc/pcapi/COMM_B2G_showClbzInfo",
        "/fccost/fcc/pcapi/COMM_B2G_QueryCommonParameterConfig",
        "/fcecf/fcc/pcapi/COMM_B2G_LoadBxdShxqz",
        "/fcecf/fcc/pcapi/COMM_B2G_GetKcxBorrowV2",
        "/fcecf/fcc/pcapi/COMM_B2G_QueryBztz",
        "/fcecf/fcc/pcapi/COMM_B2G_Dlrxx",
        "/fcecf/fcc/pcapi/COMM_B2G_LoadBlxcList",
        "/base/fcc/controlrules/getZsjsfkqByFrgsidOrYgid",
        "/hotel/fcc/hotel/order/b2ghotelzcd/getOrderDetail",
    }
)
TRIP_TEMPLATES = frozenset(
    {
        "/fcc/fcecf/defaultv2/bxdHome/modules/ycxcInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/zlrzInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/xcmxInfo.html",
        "/fcc/splc/splct.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/ccsqInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/annexInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/btns.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/subsidy/sharedHouseSubsidy.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/costDetails.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/loan.html",
        "/fcc/fcecf/defaultv2/bldHome/bldIndex.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/ccbgInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/subsidy/subsidy.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/remarkInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/fkxxList.html",
        "/fcc/fcecf/defaultv2/commons/commentsinfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/payeeList/payeeInfo.html",
        "/fcc/fcecf/default/entry/lowForm.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/deductionInfo.html",
        "/fcc/default/login/ssoError.html",
        "/fcc/fcecf/defaultv2/bxdHome/fjxxPage.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/fymxItem.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/insertTravel.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/gdInfo.html",
        "/fcc/fcecf/defaultv2/bldHome/modules/taxInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/jkdpage.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/rzbgInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/costTotal.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/fyftInfo.html",
        "/fcc/fcecf/defaultv2/bldHome/modules/baseInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/costShar.html",
        "/fcc/fcecf/defaultv2/bldHome/modules/invoiceInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/businessTripReport.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/imageInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/dkxcmxInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/baseInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/ysxxInfo.html",
        "/fcc/fcecf/defaultv2/bxdHome/modules/oasqInfo.html",
    }
)
STATIC = re.compile(
    r"\.(js|css|png|jpg|jpeg|gif|svg|ico|woff2?|ttf|eot|map|json)$", re.I
)
HOP = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class Policy:
    def __init__(self, policy):
        safe_http._origin(policy.attachments.oa_origin)
        safe_http._origin(policy.travel_origin)
        self.oa_host = urllib.parse.urlsplit(policy.attachments.oa_origin).hostname
        self.trip_host = urllib.parse.urlsplit(policy.travel_origin).hostname
        for origin in [policy.attachments.oa_origin, policy.travel_origin]:
            if (urllib.parse.urlsplit(origin).port or 443) != 443:
                raise ValueError("travel_read_origin_port_unsupported")
        self.hosts = {self.oa_host, self.trip_host}
        self.files = frozenset()

    def allows(self, method, host, path):
        if host not in self.hosts:
            return False
        if method not in {"GET", "HEAD", "POST"}:
            return False
        decoded = urllib.parse.unquote(path)
        if (
            ".." in decoded.split("/")
            or "\\" in decoded
            or any((ord(c) < 32 for c in decoded))
            or re.search("%2f|%5c", path, re.I)
            or re.search("%[0-9a-f]{2}", decoded, re.I)
        ):
            return False
        if host == self.oa_host:
            if path in OA_READ:
                return True
            if method not in {"GET", "HEAD"}:
                return False
            if path in {"/wui/index.html", "/sn_slv_bxbz.jsp"}:
                return True
            return bool(
                STATIC.search(path)
                and path.startswith(
                    (
                        "/cloudstore/",
                        "/spa/",
                        "/wui/",
                        "/js/",
                        "/formmode/",
                        "/font/",
                        "/security/js/",
                        "/mobile/languagelocal/",
                        "/integration/js/",
                        "/social/im/js/",
                        "/page/resource/",
                    )
                )
            )
        if host == self.trip_host:
            if path in TRIP_READ and method in {"GET", "POST"}:
                return True
            if method not in {"GET", "HEAD"}:
                return False
            if (
                path in {"/fcc/fcecf/bxdHome/index.html", "/fcopen/fcsso"}
                or path in TRIP_TEMPLATES
            ):
                return True
            file_match = re.fullmatch(
                r"/fcfile/fcc/opera/(getFile|share/open)/([0-9]+)/.*", path
            )
            if file_match:
                return file_match[2] in self.files
            return bool(
                STATIC.search(path)
                and path.startswith(("/static/", "/fcc/", "/resource/", "/assets/"))
                and ("/pcapi/" not in path)
            )
        return False


class ReadonlyProxy:
    def __init__(self, policy, upstream=None, *, patches=True):
        self.directory = tempfile.TemporaryDirectory(prefix="oa_travel_read_")
        root = Path(self.directory.name)
        self.cert = root / "proxy.crt"
        self.key = root / "proxy.key"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(self.key),
                "-out",
                str(self.cert),
                "-subj",
                "/CN=OA read session",
                "-addext",
                "subjectAltName=" + ",".join("DNS:" + h for h in sorted(policy.hosts)),
            ],
            check=True,
            capture_output=True,
        )
        self.key.chmod(0o600)
        self.patches = []
        self.policy = policy
        self.upstream = upstream
        self.records = []
        self.lock = threading.Lock()
        self.sequence = 0
        self.active = 0
        self.started = time.monotonic()
        self.last_activity = self.started
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, headers, body):
                self.send_response_only(status)
                for k, v in headers:
                    if k.lower() not in HOP | {"content-length"}:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                self.close_connection = True

            def do_CONNECT(self):
                if self.path not in {h + ":443" for h in outer.policy.hosts}:
                    outer.record(
                        {
                            "method": "CONNECT",
                            "host": self.path.split(":")[0],
                            "path": "",
                            "decision": "blocked_host",
                        }
                    )
                    self.reply(403, [], b"Blocked")
                    return
                self.connect_host = self.path[:-4]
                self.send_response_only(200)
                self.end_headers()
                try:
                    self.connection = outer.tls.wrap_socket(
                        self.connection, server_side=True
                    )
                    self.rfile = self.connection.makefile("rb", self.rbufsize)
                    self.wfile = self.connection.makefile("wb", self.wbufsize)
                    self.close_connection = False
                    self.handle_one_request()
                except (OSError, ssl.SSLError):
                    self.close_connection = True

            def deliver(self, status, headers, body, entry):
                try:
                    self.reply(
                        status,
                        [
                            (k, v)
                            for k, v in headers
                            if k.lower()
                            not in {"x-oa-readonly-request-id", "x-oa-readonly-blocked"}
                        ]
                        + [("X-OA-Readonly-Request-Id", entry["request_id"])]
                        + (
                            [("X-OA-Readonly-Blocked", "1")]
                            if entry.get("blocked_by_readonly")
                            else []
                        ),
                        body,
                    )
                except (OSError, ssl.SSLError) as error:
                    outer.record(
                        {
                            **entry,
                            "decision": "client_write_error",
                            "phase": "client_write",
                            "status": status,
                            "bytes_planned": len(body),
                            "error_type": type(error).__name__,
                        }
                    )
                except Exception as error:
                    outer.record(
                        {
                            **entry,
                            "decision": "client_response_error",
                            "phase": "client_write",
                            "status": status,
                            "error_type": type(error).__name__,
                        }
                    )
                else:
                    outer.record(
                        {
                            **entry,
                            "decision": "client_response_complete",
                            "phase": "client_write",
                            "status": status,
                            "bytes": 0 if self.command == "HEAD" else len(body),
                        }
                    )
                self.close_connection = True

            def forward(self):
                with outer.lock:
                    outer.sequence += 1
                    request_id = "r" + str(outer.sequence)
                    outer.active += 1
                try:
                    self.forward_request(request_id)
                finally:
                    with outer.lock:
                        outer.active -= 1
                        outer.last_activity = time.monotonic()

            def forward_request(self, request_id):
                started = time.monotonic()
                parsed = urllib.parse.urlsplit(self.path)
                host = getattr(self, "connect_host", None)
                entry = {
                    "request_id": request_id,
                    "method": self.command,
                    "host": host,
                    "path": parsed.path,
                }
                outer.record(
                    {**entry, "decision": "request_started", "phase": "client_read"}
                )
                if (
                    parsed.scheme
                    or parsed.netloc
                    or not host
                    or self.headers.get("Host", "").lower() not in {host, host + ":443"}
                ):
                    outer.record(
                        {**entry, "decision": "blocked_authority", "phase": "policy"}
                    )
                    self.deliver(
                        403, [], b"Blocked", {**entry, "blocked_by_readonly": True}
                    )
                    return
                if not outer.policy.allows(self.command, host, parsed.path):
                    outer.record({**entry, "decision": "blocked", "phase": "policy"})
                    self.deliver(
                        403,
                        [("Content-Type", "text/plain")],
                        b"Read-only test blocked this request",
                        {**entry, "blocked_by_readonly": True},
                    )
                    return
                if self.headers.get("Transfer-Encoding") or self.headers.get("Upgrade"):
                    outer.record(
                        {**entry, "decision": "blocked_transport", "phase": "policy"}
                    )
                    self.deliver(
                        403, [], b"Blocked", {**entry, "blocked_by_readonly": True}
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > 1000000:
                    outer.record(
                        {**entry, "decision": "blocked_body", "phase": "policy"}
                    )
                    self.deliver(
                        413, [], b"Blocked", {**entry, "blocked_by_readonly": True}
                    )
                    return
                try:
                    body = self.rfile.read(length) if length else None
                    if length and len(body) != length:
                        raise EOFError("short_request_body")
                except Exception as error:
                    outer.record(
                        {
                            **entry,
                            "decision": "client_read_error",
                            "phase": "client_read",
                            "error_type": type(error).__name__,
                        }
                    )
                    self.close_connection = True
                    return
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in HOP | {"host", "content-length", "expect"}
                }
                url = "https://" + host + self.path
                try:
                    if outer.upstream:
                        status, response_headers, data = outer.upstream(
                            self.command, url, headers, body
                        )
                    else:
                        request = urllib.request.Request(
                            url, headers=headers, data=body, method=self.command
                        )
                        opener = urllib.request.build_opener(
                            urllib.request.ProxyHandler({}), NoRedirect()
                        )
                        try:
                            response = opener.open(request, timeout=20)
                        except urllib.error.HTTPError as error:
                            response = error
                        with response:
                            status = response.status
                            response_headers = list(response.headers.raw_items())
                            data = response.read(30000001)
                        if len(data) > 30000000:
                            raise RuntimeError("response_limit")
                except Exception as error:
                    outer.record(
                        {
                            **entry,
                            "decision": "upstream_error",
                            "phase": "upstream_read",
                            "error_type": type(error).__name__,
                        }
                    )
                    self.deliver(502, [], b"Upstream read failed", entry)
                    return
                try:
                    data, response_headers, patch = (
                        patch_response(
                            path=parsed.path,
                            host=host,
                            trip_host=outer.policy.trip_host,
                            status=status,
                            data=data,
                            headers=response_headers,
                        )
                        if patches
                        else (data, response_headers, None)
                    )
                except ValueError as error:
                    outer.record(
                        {
                            **entry,
                            "decision": "compatibility_error",
                            "phase": "compatibility",
                            "reason": str(error),
                        }
                    )
                    self.deliver(502, [], b"Compatibility version unsupported", entry)
                    return
                if patch:
                    outer.patches.append(patch)
                outer.record(
                    {
                        **entry,
                        "decision": "forwarded",
                        "phase": "upstream_read",
                        "status": status,
                        "bytes": len(data),
                        "sec": round(time.monotonic() - started, 3),
                    }
                )
                self.deliver(status, response_headers, data, entry)

            do_GET = forward
            do_HEAD = forward
            do_POST = forward
            do_PUT = forward
            do_PATCH = forward
            do_DELETE = forward
            do_OPTIONS = forward

        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(self.cert, self.key)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def record(self, item):
        with self.lock:
            self.records.append(
                {**item, "at_sec": round(time.monotonic() - self.started, 6)}
            )

    def start(self):
        self.thread.start()
        return "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.close_drained = self.wait_for_idle(timeout=22, quiet=0)
        self.server.server_close()
        self.thread.join(2)
        self.directory.cleanup()

    def wait_for_idle(self, timeout=2, quiet=0.2):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            with self.lock:
                idle = (
                    self.active == 0 and time.monotonic() - self.last_activity >= quiet
                )
            if idle:
                return True
            time.sleep(0.02)
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
