from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from apps.oa.approvals.sources import auth
from apps.oa.approvals.sources import browser_session as rpa_session
from apps.oa.runtime import control as runtime_control
from apps.oa.approvals.sources.download_errors import detect_oa_download_fail_code


class _DocumentContextRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        failure = detect_oa_download_fail_code(newurl) or rpa_session.RPA_SESSION_INVALID_CODE
        raise rpa_session.RpaSessionError(failure, '原生文档接口发生重定向')


def accepts(resource: dict, url: str, *, base_url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlsplit(base_url)
    request_id = str(resource.get("_workflow_request_id") or "")
    file_id = str(resource.get("id") or "")
    cookie = str(resource.get("_ephemeral_cookie_header") or "")
    url_ids = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("fileid", [])
    return not (
        (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc)
        or parsed.path not in {"/weaver/weaver.file.FileDownload", "/news/download.do"}
        or len(url_ids) != 1 or not url_ids[0].strip()
        or (re.fullmatch(r"[0-9]+", url_ids[0]) and url_ids[0] != file_id)
        or not re.fullmatch(r"[1-9][0-9]*", request_id)
        or not cookie
    )


def verify(resource: dict, url: str, *, base_url: str, document_id: str,
           timeout_sec: int, initialize: bool = True) -> bool:
    if not accepts(resource, url, base_url=base_url):
        return False
    request_id = str(resource.get('_workflow_request_id') or '')
    file_id = str(resource.get('id') or '')
    cookie = str(resource.get('_ephemeral_cookie_header') or '')

    if not document_id:
        return False

    def read(path: str, params: dict[str, str] | None = None) -> dict:
        query = "?" + urllib.parse.urlencode(params) if params else ""
        request = urllib.request.Request(
            base_url + path + query,
            headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie},
        )
        opener = urllib.request.build_opener(_DocumentContextRedirect())
        with opener.open(request, timeout=max(1, timeout_sec)) as response:
            payload = json.loads(response.read())
        return payload if isinstance(payload, dict) else {}

    try:
        account = read("/api/hrm/login/getAccountList")
        actual_user = str((account.get("data") or {}).get("userid") or "")
        expected_user = auth.env("OA_UID")
        if not expected_user or actual_user != expected_user:
            raise rpa_session.RpaSessionError(
                rpa_session.RPA_SESSION_INVALID_CODE, "RPA 文档读取账号与配置不一致"
            )
        preview = read("/api/doc/detail/basicInfo", {"docid": document_id})
        if str(preview.get("status")) != "1" or str(preview.get("mainimagefile")) != file_id:
            return False
        if not initialize:
            return True
        if runtime_control.stop_requested():
            raise rpa_session.RpaSessionError(runtime_control.STOP_CODE, "已请求停止")
        info = read(
            "/api/doc/detail/basicInfo",
            {"docid": document_id, "isrequest": "1", "requestid": request_id},
        )
        return str(info.get("status")) == "1" and str(info.get("mainimagefile")) == file_id
    except rpa_session.RpaSessionError:
        raise
    except Exception:
        return False
