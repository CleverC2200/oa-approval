from datetime import datetime
from io import BytesIO
import json
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from apps.oa.approvals.sources import http as safe_http
from apps.oa.approvals.sources import writer as writer_api
from apps.oa.approvals.sources.oa import HANDLED_ENDPOINTS
from apps.oa.approvals.sources.oa import TODO_ENDPOINT
from apps.oa.approvals.sources.writer import FrozenWriterApi
from apps.oa.approvals.sources.writer import SUBMIT_ENDPOINT


@pytest.mark.parametrize('target_page', [2, 6])
def test_writer_finds_current_receipt_after_old_receipt_and_fifth_page(monkeypatch, target_page):
    posts = []
    monkeypatch.setattr(writer_api.time, 'sleep', lambda _: None)
    def transport(request, **_kwargs):
        path = urlparse(request.full_url).path
        if path == SUBMIT_ENDPOINT:
            posts.append(request.data)
            result = {'code': 'SUCCESS'}
        elif path == TODO_ENDPOINT:
            result = {'data': []}
        else:
            assert path in HANDLED_ENDPOINTS
            page = int(parse_qs(request.data.decode())['pageNo'][0])
            rows = [{'requestId': str(page * 1000 + index), 'operateDate': '2020-01-01',
                     'operateTime': '00:00:00'} for index in range(100)]
            if page == 1:
                rows[0]['requestId'] = '101'
            if page == target_page:
                value = datetime.now(ZoneInfo('Asia/Shanghai'))
                rows = [{'requestId': '101', 'operateDate': value.strftime('%Y-%m-%d'),
                         'operateTime': value.strftime('%H:%M:%S')}]
            result = {'data': rows}
        return BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(safe_http, 'urlopen', transport)
    writer = FrozenWriterApi('fake-token', 'fake-user', app_id='fake-app', base_url='https://oa.example.test')
    result = writer.submit_and_verify('101', 'Synthetic opinion')
    assert result == {'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True, 'failure_code': None}
    assert len(posts) == 1


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
