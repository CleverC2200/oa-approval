from __future__ import annotations

import gzip
import http.cookiejar
import json
import urllib.parse
import urllib.request

from apps.oa.approvals.sources import auth, http as safe_http


ACCOUNT_PATH = "/api/hrm/login/getAccountList"
BOOTSTRAP_PATH = "/api/workflow/paService/getToDoWorkflowRequestList"


class ApiSessionError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def verified_identity(payload, expected_uid):
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if not isinstance(data, dict) or not isinstance(data.get("accountlist", []), list):
        return False
    current = [
        r
        for r in data.get("accountlist", [])
        if isinstance(r, dict) and str(r.get("iscurrent")) == "1"
    ]
    if not current and data.get("userid") is not None:
        current = [data]
    return bool(expected_uid) and [str(r.get("userid")) for r in current] == [
        str(expected_uid)
    ]


def api_storage_state(policy, *, timeout_sec):
    base = policy.attachments.oa_origin
    safe_http._origin(base)
    token, encrypted_uid = auth.credentials(base_url=base)
    jar = http.cookiejar.CookieJar()

    def request(path, *, body=None, headers=None):
        req = urllib.request.Request(base + path, data=body, headers=headers or {})
        jar.add_cookie_header(req)
        with safe_http.urlopen(req, timeout=max(0.1, timeout_sec)) as response:
            jar.extract_cookies(response, req)
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8-sig"))

    reply = request(
        BOOTSTRAP_PATH,
        body=urllib.parse.urlencode(
            {
                "pageNo": 1,
                "pageSize": 1,
                "isNeedOs": True,
            }
        ).encode(),
        headers={
            "appid": auth.env("OA_APP_ID"),
            "token": token,
            "userid": encrypted_uid,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    valid_reply = (
        isinstance(reply, dict) and reply.get("code") == "SUCCESS"
    ) or (isinstance(reply, list) and all(isinstance(row, dict) for row in reply))
    if not valid_reply:
        raise ApiSessionError("travel_api_session_bootstrap_failed")
    if not verified_identity(request(ACCOUNT_PATH), auth.env("OA_UID")):
        raise ApiSessionError("travel_session_identity_mismatch")
    host = urllib.parse.urlsplit(base).hostname
    cookies = []
    for cookie in jar:
        if cookie.domain.lstrip(".") != host:
            continue
        item = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": cookie.secure,
            "httpOnly": any(k.lower() == "httponly" for k in cookie._rest),
        }
        if cookie.expires:
            item["expires"] = cookie.expires
        cookies.append(item)
    jar.clear()
    if not cookies:
        raise ApiSessionError("travel_api_session_cookie_missing")
    return {"cookies": cookies, "origins": []}
