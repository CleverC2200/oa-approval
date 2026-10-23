from datetime import datetime
from io import BytesIO
import json
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pytest

from apps.oa_approval import safe_http
from apps.oa_approval.api import HANDLED_ENDPOINTS, TODO_ENDPOINT
from apps.oa_approval.writer_api import FrozenWriterApi, SUBMIT_ENDPOINT


@pytest.mark.parametrize("handled_time, confirmed", [(None, False), ("2020-01-01 12:00:00", False), ("fresh", True)])
def test_writer_requires_current_attempt_handled_evidence(monkeypatch, handled_time, confirmed):
    posts = []
    def transport(request, **kwargs):
        path = urlparse(request.full_url).path
        if path == SUBMIT_ENDPOINT:
            posts.append(request.data)
            result = {"code":"SUCCESS"}
        elif path == TODO_ENDPOINT:
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
