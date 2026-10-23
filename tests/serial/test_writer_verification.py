from datetime import datetime
from io import BytesIO
import json
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pytest

from apps.oa_approval import safe_http, writer_api
from apps.oa_approval.api import HANDLED_ENDPOINTS, TODO_ENDPOINT
from apps.oa_approval.writer_api import FrozenWriterApi, SUBMIT_ENDPOINT


@pytest.mark.parametrize("handled_time, confirmed", [(None, False), ("2020-01-01 12:00:00", False), ("fresh", True)])
def test_writer_requires_current_attempt_handled_evidence(monkeypatch, handled_time, confirmed):
    posts, waits, pending_reads = [], [], []
    monkeypatch.setattr(writer_api.time, "sleep", waits.append)
    def transport(request, **kwargs):
        path = urlparse(request.full_url).path
        if path == SUBMIT_ENDPOINT:
            posts.append(request.data)
            result = {"code":"SUCCESS"}
        elif path == TODO_ENDPOINT:
            pending_reads.append(path)
            result = {"data":[]}
        else:
            assert path in HANDLED_ENDPOINTS
            value = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S") if handled_time == "fresh" else handled_time
            date, _, time = (value or "").partition(" ")
            result = {"data":[{"requestId":"101","operateDate":date,"operateTime":time}]}
        return BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(safe_http,"urlopen",transport)
    writer = FrozenWriterApi("fake-token","fake-user",app_id="fake-app",base_url="https://oa.example.test")
    result = writer.submit_and_verify("101","意见")
    assert result["postcheck_done_hit"] is confirmed
    assert result["done_hit"] is confirmed
    assert len(posts) == 1
    assert len(pending_reads) == (1 if confirmed else 3)
    assert waits == ([] if confirmed else [0.25, 0.25])


@pytest.mark.parametrize("response,pending,expected_code", [
    ("ERROR", False, "writer_submit_rejected"),
    ("UNRECOGNIZED", False, "writer_transport_unknown"),
    ("SUCCESS", True, "writer_pending_conflict"),
    ("SUCCESS", False, None),
])
def test_writer_preserves_response_and_pending_evidence(monkeypatch, response, pending, expected_code):
    posts = []
    def transport(request, **kwargs):
        path = urlparse(request.full_url).path
        if path == SUBMIT_ENDPOINT:
            posts.append(request.data)
            result = {"code": response}
        elif path == TODO_ENDPOINT:
            result = {"data": [{"requestId": "101"}] if pending else []}
        else:
            assert path in HANDLED_ENDPOINTS
            value = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
            date, _, time = value.partition(" ")
            result = {"data": [{"requestId": "101", "operateDate": date, "operateTime": time}]}
        return BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(safe_http, "urlopen", transport)
    writer = FrozenWriterApi("fake-token", "fake-user", app_id="fake-app", base_url="https://oa.example.test")
    result = writer.submit_and_verify("101", "意见")
    assert result == {"submit_ok": response == "SUCCESS", "done_hit": response == "SUCCESS",
                      "postcheck_done_hit": response == "SUCCESS" and not pending,
                      "failure_code": expected_code}
    assert len(posts) == 1
