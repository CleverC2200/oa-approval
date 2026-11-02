import base64
from dataclasses import replace
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from apps.oa.approvals.source_policy import TravelReadPolicy
from apps.oa.approvals.sources import travel_read_auth as session_auth
from apps.oa.approvals.sources import travel_read_session as sessions
from apps.oa.approvals.sources.downloads import DownloadTransfer
from apps.oa.approvals.sources.download_errors import Failed, Downloaded, Unavailable
from apps.oa.approvals.sources.travel_evidence import (
    CandidateObservation,
    TravelEvidence,
)
from apps.oa.approvals.sources.travel_page_compat import (
    source_data,
    ReadFailure,
    BASE_PATH,
    EXP_PATH,
)
from apps.oa.approvals.sources.travel_read_transport import Policy, ReadonlyProxy
from tests.approvals.sources.test_source_policy import synthetic_policy


def policy(engine="moli"):
    old = synthetic_policy()
    return replace(
        old,
        travel_read=TravelReadPolicy(engine, "api"),
        attachments=replace(
            old.attachments, extra_hosts=frozenset({"trip.example.test"})
        ),
    )


def test_defaults_and_explicit_selection():
    assert not synthetic_policy().travel_read.enabled
    assert policy().travel_read.enabled
    assert policy("chromium").travel_read.enabled
    with pytest.raises(ValueError, match="travel_browser_engine_invalid"):
        TravelReadPolicy("other")
    with pytest.raises(ValueError, match="travel_session_source_invalid"):
        TravelReadPolicy(session_source="other")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"data": None},
        {"data": []},
        {"data": {"accountlist": [{"userid": "8", "iscurrent": "1"}]}},
    ],
)
def test_identity_fails_closed_on_malformed_or_wrong_user(payload):
    assert not session_auth.verified_identity(payload, "7")


@pytest.mark.parametrize("bootstrap_reply", [{"code": "SUCCESS"}, []])
def test_api_bootstrap_reuses_registration_keeps_cookie_private(
    monkeypatch, bootstrap_reply
):
    from email.message import Message

    requests = []
    monkeypatch.setattr(
        session_auth.auth,
        "credentials",
        lambda **kw: ("synthetic-token", "synthetic-encrypted-uid"),
    )
    monkeypatch.setattr(
        session_auth.auth,
        "env",
        lambda name: {"OA_APP_ID": "synthetic-app", "OA_UID": "7"}[name],
    )

    class Response(BytesIO):
        def __init__(self, body, cookie=""):
            super().__init__(json.dumps(body).encode())
            self.headers = Message()
            if cookie:
                self.headers["Set-Cookie"] = cookie

        def info(self):
            return self.headers

    def request(req, **kwargs):
        requests.append(req)
        if req.full_url.endswith(session_auth.BOOTSTRAP_PATH):
            return Response(
                bootstrap_reply,
                "JSESSIONID=synthetic-private; Path=/; Secure; HttpOnly",
            )
        assert "synthetic-private" in req.get_header("Cookie")
        return Response({"data": {"accountlist": [{"userid": "7", "iscurrent": "1"}]}})

    monkeypatch.setattr(session_auth.safe_http, "urlopen", request)
    state = session_auth.api_storage_state(policy(), timeout_sec=5)
    assert len(requests) == 2
    assert requests[0].get_header("Appid") == "synthetic-app"
    assert b"pageSize=1" in requests[0].data
    assert state["cookies"][0]["httpOnly"]
    assert state["cookies"][0]["domain"] == "oa.example.test"
    assert not any("register" in r.full_url for r in requests)


@pytest.mark.parametrize(
    "reason,retryable,expected",
    [
        ("travel_moli_binary_missing", True, ["moli", "chromium"]),
        ("travel_dom_not_ready", True, ["moli", "chromium"]),
        ("travel_session_identity_mismatch", False, ["moli"]),
        ("travel_upstream_access_denied", False, ["moli"]),
        ("travel_source_binding_mismatch", False, ["moli"]),
        ("travel_required_request_blocked", False, ["moli"]),
    ],
)
def test_fallback_is_once_and_only_for_engine_errors(
    monkeypatch, reason, retryable, expected
):
    monkeypatch.setattr(sessions, "api_storage_state", lambda *a, **kw: {"cookies": []})
    session = sessions.TravelReadSession(policy())
    calls = []

    def attempt(engine, state, urls, marker, count, deadline, **kwargs):
        calls.append(engine)
        if engine == "moli":
            raise ReadFailure(reason, retryable=retryable)
        return CandidateObservation(text="CASE-A", scan_status="empty")

    monkeypatch.setattr(session, "_attempt", attempt)
    try:
        results = session.read_attempt(
            ["https://oa.example.test/sn_slv_bxbz.jsp"],
            timeout_sec=10,
            expected_marker="CASE-A",
            expected_resource_count=0,
        )
        assert calls == expected
        if len(expected) == 1:
            assert results[0].diagnostics[0]["reason"] == reason
        else:
            assert results[0].text == "CASE-A"
    finally:
        session.close()
    assert session.closed and not session.files and not session.payloads


def test_policy_blocks_writes_and_requires_bound_download_file():
    rules = Policy(policy())
    for method, host, path in [
        ("POST", rules.oa_host, "/api/workflow/paService/submitRequest"),
        ("POST", rules.trip_host, "/fcecf/fcc/pcapi/COMM_B2G_SaveBxd"),
        ("GET", "untrusted.example.test", BASE_PATH),
        ("DELETE", rules.trip_host, BASE_PATH),
        ("GET", rules.trip_host, "/fcc/%252e%252e/data.json"),
    ]:
        assert not rules.allows(method, host, path)
    assert rules.allows("POST", rules.trip_host, BASE_PATH)
    path = "/fcfile/fcc/opera/getFile/123/fixture.jpg"
    assert not rules.allows("GET", rules.trip_host, path)
    rules.files = frozenset({"123"})
    assert rules.allows("GET", rules.trip_host, path)
    assert not rules.allows("POST", rules.trip_host, path)
    for host, path in [
        (rules.trip_host, "/fcc/action/delete.json"),
        (rules.trip_host, "/fcc/action/delete.js"),
        (rules.oa_host, "/spa/action/delete.js"),
    ]:
        assert not rules.allows("GET", host, path)


@pytest.mark.parametrize(
    "blocked_path",
    [
        None,
        "/fcecf/fcc/pcapi/COMM_B2G_SaveBxd",
        "/fcc/action/delete.json",
        "/fcc/action/delete.js",
    ],
)
def test_wire_proxy_blocks_before_upstream_and_labels_local_403(
    monkeypatch, blocked_path
):
    import socket
    import ssl
    import threading

    locally_blocked = blocked_path is not None
    monkeypatch.setattr(socket, "getfqdn", lambda *args: "localhost")
    forwarded = []

    def upstream(*args):
        forwarded.append(args)
        return (
            403,
            [("X-OA-Readonly-Blocked", "1"), ("X-OA-Readonly-Request-Id", "forged")],
            b"Server denied",
        )

    proxy = ReadonlyProxy(Policy(policy()), upstream=upstream, patches=False)
    proxy.start()
    try:
        sock, server_socket = socket.socketpair()
        worker = threading.Thread(
            target=proxy.server.RequestHandlerClass,
            args=(server_socket, ("127.0.0.1", 0), proxy.server),
        )
        worker.start()
        with sock:
            sock.sendall(
                b"CONNECT trip.example.test:443 HTTP/1.1\r\nHost: trip.example.test:443\r\n\r\n"
            )
            assert b"200" in sock.recv(4096)
            tls = ssl.create_default_context(cafile=str(proxy.cert)).wrap_socket(
                sock, server_hostname="trip.example.test"
            )
            request_path = blocked_path or BASE_PATH
            method = (
                "GET"
                if blocked_path and blocked_path.endswith((".json", ".js"))
                else "POST"
            )
            tls.sendall(
                (
                    method
                    + " "
                    + request_path
                    + " HTTP/1.1\r\nHost: trip.example.test\r\nContent-Length: 0\r\n\r\n"
                ).encode()
            )
            response = b""
            expected_body = (
                b"Read-only test blocked this request"
                if locally_blocked
                else b"Server denied"
            )
            while expected_body not in response:
                data = tls.recv(4096)
                assert data
                response += data
            tls.close()
        worker.join(timeout=2)
        server_socket.close()
        assert not worker.is_alive()
        assert b"403" in response
        assert (b"X-OA-Readonly-Blocked: 1" in response) == locally_blocked
        assert b"forged" not in response
        assert len(forwarded) == (0 if locally_blocked else 1)
        assert any(r["decision"] == "client_response_complete" for r in proxy.records)
    finally:
        proxy.close()
    assert not proxy.cert.exists() and not proxy.thread.is_alive()


def test_source_supplement_preserves_zero_time_and_ownership():
    payloads = [
        {
            "path": BASE_PATH,
            "status": 200,
            "payload": {
                "success": True,
                "result": {"bxdzb": {"bxdh": "CASE-A", "bxpch": "BATCH-A"}},
            },
        },
        {
            "path": EXP_PATH,
            "status": 200,
            "payload": {
                "success": True,
                "result": {
                    "bxmxList": [
                        {
                            "id": "ROW-A",
                            "bxpch": "BATCH-A",
                            "se": 0,
                            "xcrqs": "2026-10-01 09:30:00",
                        }
                    ]
                },
            },
        },
    ]
    _, _, facts = source_data(payloads, "CASE-A")
    assert [f["value"] for f in facts] == ["0", "2026-10-01 09:30:00"]
    assert all(f["subject_ref"] == "travel_expense:ROW-A" for f in facts)
    evidence = TravelEvidence.assemble(
        [
            CandidateObservation(
                text="CASE-A", scan_status="empty", source_facts=tuple(facts)
            )
        ],
        fields=[],
        request_id="7",
        flow_mark="CASE-A",
        expected_resource_count=0,
    )
    assert any(
        f.get("field") == "xcrqs" and f["source_json_path"] for f in evidence.facts
    )
    payloads[1]["payload"]["result"]["bxmxList"][0]["bxpch"] = "OTHER"
    with pytest.raises(ReadFailure, match="travel_source_row_binding_mismatch"):
        source_data(payloads, "CASE-A")


@pytest.mark.parametrize("decision", ["client_write_error", "client_response_error"])
def test_material_delivery_error_is_not_mistaken_for_success(decision):
    session = sessions.TravelReadSession(policy())
    session.transport = SimpleNamespace(
        records=[
            {"path": BASE_PATH, "decision": decision, "request_id": "r1", "status": 200}
        ],
        wait_for_idle=lambda **kw: True,
    )
    with pytest.raises(ReadFailure, match="travel_delivery_incomplete"):
        session._verify_delivery(["r1"])
    session.close()


def test_download_stays_on_owner_thread_and_does_not_fallback(tmp_path, monkeypatch):
    session = sessions.TravelReadSession(policy())
    resource = {
        "id": "travel_platform:123",
        "source": "travel_platform",
        "name": "fixture.jpg",
        "downloadUrl": "https://trip.example.test/fcfile/fcc/opera/getFile/123/fixture.jpg",
    }
    rules = Policy(policy())
    rules.files = frozenset({"123"})
    session.files = frozenset({"123"})
    session.transport = SimpleNamespace(policy=rules)
    monkeypatch.setattr(session, "_verify_oa", lambda deadline: None)
    monkeypatch.setattr(session, "_verify_trip", lambda: None)
    monkeypatch.setattr(session, "_verify_delivery", lambda ids: None)
    session.page = SimpleNamespace(
        evaluate=lambda *a: {
            "status": 200,
            "type": "image/jpeg",
            "request_id": "r1",
            "data": base64.b64encode(b"synthetic-bytes").decode(),
        }
    )
    transfer = DownloadTransfer(policy(), travel_session=session)
    assert isinstance(
        transfer.direct(resource, dest_dir=tmp_path, timeout_sec=5), Unavailable
    )
    assert isinstance(
        transfer.browser(resource, dest_dir=tmp_path, timeout_sec=5), Downloaded
    )
    form_resource = {**resource, "id": "field_main_1_1"}
    del form_resource["source"]
    assert isinstance(
        transfer.direct(form_resource, dest_dir=tmp_path, timeout_sec=5), Unavailable
    )
    assert isinstance(
        transfer.browser(form_resource, dest_dir=tmp_path, timeout_sec=5), Downloaded
    )
    wrong = {**resource, "id": "travel_platform:456"}
    assert isinstance(transfer.browser(wrong, dest_dir=tmp_path, timeout_sec=5), Failed)
    assert not session.attempts
    session.close()


def test_successful_fallback_diagnostics_reach_material_scan():
    diagnostics = (
        {"stage": "engine", "browser": "moli", "reason": "travel_engine_start_failed"},
        {
            "stage": "engine",
            "browser": "chromium",
            "requested_engine": "moli",
            "effective_engine": "chromium",
            "version": "fixture-version",
        },
    )
    evidence = TravelEvidence.assemble(
        [
            CandidateObservation(
                text="CASE-A", scan_status="empty", diagnostics=diagnostics
            )
        ],
        fields=[],
        request_id="7",
        flow_mark="CASE-A",
        expected_resource_count=0,
    )
    assert evidence.attachment_scan_record()["diagnostics"] == list(diagnostics)


@pytest.mark.parametrize(
    "new_decision,idle",
    [
        ("client_write_error", True),
        ("request_started", False),
        ("client_response_complete", True),
    ],
)
def test_unobserved_new_material_request_is_not_hidden_by_old_success(
    new_decision, idle
):
    session = sessions.TravelReadSession(policy())
    session.payloads = [
        {"path": BASE_PATH, "payload": {"success": True}, "request_id": "r1"}
    ]
    records = [
        {
            "path": BASE_PATH,
            "decision": "client_response_complete",
            "request_id": "r1",
            "status": 200,
        },
        {"path": BASE_PATH, "decision": "request_started", "request_id": "r2"},
        {
            "path": BASE_PATH,
            "decision": new_decision,
            "request_id": "r2",
            "status": 200,
        },
    ]
    session.transport = SimpleNamespace(
        records=records, wait_for_idle=lambda **kw: idle
    )
    with pytest.raises(ReadFailure, match="travel_delivery_"):
        session._verify_delivery(["r1"])
    session.close()


def test_upstream_slow_trickle_has_a_whole_request_deadline(monkeypatch):
    from email.message import Message
    from apps.oa.approvals.sources import travel_read_transport as transport

    clock = [1.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    closed = []
    timeouts = []

    class Response:
        status = 200
        headers = Message()

        def close(self):
            pass

        def read1(self, _size):
            clock[0] += 7
            return b"x"

    class Connection:
        sock = SimpleNamespace(settimeout=lambda timeout: timeouts.append(timeout))

        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            self.sock = None
            return Response()

        def close(self):
            closed.append(True)

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    proxy = object.__new__(ReadonlyProxy)
    import threading

    proxy.upstream_sockets = {}
    proxy.lock, proxy.connections, proxy.closing, proxy.deadline = (
        threading.Lock(),
        set(),
        threading.Event(),
        100,
    )
    with pytest.raises(TimeoutError, match="upstream_deadline"):
        proxy.read_upstream(
            "GET", "https://trip.example.test/static/core/fixture.js", {}, None
        )
    assert clock[0] == 22 and closed and not proxy.connections
    assert timeouts == [20, 13, 6]


def test_local_moli_startup_timeout_is_retryable_while_total_budget_remains(
    monkeypatch,
):
    clock = [1.0]
    monkeypatch.setattr(sessions.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        sessions.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def unavailable(*args, **kwargs):
        raise OSError("fixture_not_ready")

    monkeypatch.setattr(
        sessions.urllib.request,
        "build_opener",
        lambda *a: SimpleNamespace(open=unavailable),
    )
    session = sessions.TravelReadSession(policy())
    with pytest.raises(ReadFailure) as error:
        session._wait_for_moli(
            SimpleNamespace(poll=lambda: None), "http://127.0.0.1:1234", 100
        )
    assert error.value.code == "travel_moli_start_timeout" and error.value.retryable
    assert clock[0] < 12
    session.close()


def test_upstream_header_wait_is_cancelled_at_total_deadline(monkeypatch):
    import threading
    import time
    from apps.oa.approvals.sources import travel_read_transport as transport

    cancelled = threading.Event()

    class Connection:
        sock = SimpleNamespace(shutdown=lambda how: cancelled.set())

        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            assert cancelled.wait(1)
            raise OSError("fixture_read_cancelled")

        def close(self):
            pass

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    proxy = object.__new__(ReadonlyProxy)
    proxy.lock, proxy.connections, proxy.upstream_sockets, proxy.closing = (
        threading.Lock(),
        set(),
        {},
        threading.Event(),
    )
    started = time.monotonic()
    proxy.deadline = started + 0.1
    with pytest.raises(OSError, match="fixture_read_cancelled"):
        proxy.read_upstream(
            "GET", "https://trip.example.test/static/core/fixture.js", {}, None
        )
    assert (
        time.monotonic() - started < 1 and cancelled.is_set() and not proxy.connections
    )


def test_close_cancels_active_upstream_connections(monkeypatch):
    import socket
    import threading

    monkeypatch.setattr(socket, "getfqdn", lambda *args: "localhost")
    proxy = ReadonlyProxy(Policy(policy()), patches=False)
    cancelled = threading.Event()
    connection = SimpleNamespace(
        sock=SimpleNamespace(shutdown=lambda how: cancelled.set()),
        close=lambda: cancelled.set(),
    )
    proxy.connections = [connection]
    proxy.active = 1
    proxy.start()

    def worker():
        assert cancelled.wait(2)
        with proxy.lock:
            proxy.active = 0
            proxy.connections = []

    thread = threading.Thread(target=worker)
    thread.start()
    proxy.close()
    thread.join(2)
    assert proxy.close_drained and cancelled.is_set() and not thread.is_alive()


def test_shared_deadline_is_not_reset_for_fallback(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(sessions.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sessions, "api_storage_state", lambda *a, **kw: {"cookies": []})
    session = sessions.TravelReadSession(policy())
    calls = []

    def attempt(engine, state, urls, marker, count, deadline, **kwargs):
        calls.append((engine, deadline))
        if engine == "moli":
            clock[0] = 17
            raise ReadFailure("travel_dom_not_ready", retryable=True)
        return CandidateObservation(text="CASE-A", scan_status="empty")

    monkeypatch.setattr(session, "_attempt", attempt)
    session.read_attempt(
        [], timeout_sec=10, expected_marker="CASE-A", expected_resource_count=0
    )
    assert calls == [("moli", 16.0), ("chromium", 20.0)]
    session.close()


def test_stop_does_not_start_another_engine(monkeypatch):
    monkeypatch.setattr(sessions, "api_storage_state", lambda *a, **kw: {"cookies": []})
    session = sessions.TravelReadSession(policy())
    calls = []

    def attempt(*args, **kwargs):
        calls.append(args[0])
        raise ReadFailure(sessions.control.STOP_CODE)

    monkeypatch.setattr(session, "_attempt", attempt)
    session.read_attempt(
        [], timeout_sec=10, expected_marker="CASE-A", expected_resource_count=0
    )
    assert calls == ["moli"]
    session.close()


def test_unknown_page_script_is_not_patched():
    from apps.oa.approvals.sources.travel_page_patch import COST_PATH, patch_response

    with pytest.raises(ValueError, match="travel_compatibility_version_mismatch"):
        patch_response(
            path=COST_PATH,
            host="trip.example.test",
            trip_host="trip.example.test",
            status=200,
            data=b"unrecognized-script",
            headers=[],
        )


def test_collector_closes_read_session_even_on_pre_discovery_failure(monkeypatch):
    from apps.oa.approvals.collection.case import collect_case

    session = sessions.TravelReadSession(policy())
    monkeypatch.setattr(sessions, "TravelReadSession", lambda _: session)
    api = SimpleNamespace(
        request_context=lambda *_: (_ for _ in ()).throw(
            RuntimeError("fixture_read_failed")
        )
    )
    with pytest.raises(RuntimeError, match="fixture_read_failed"):
        collect_case(
            "fixture-request",
            oa_api=api,
            source_policy=policy(),
            configs=[],
            allow_browser_download=True,
        )
    assert session.closed
