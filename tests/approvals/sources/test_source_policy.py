from pathlib import Path

from apps.oa.approvals.source_policy import (
    AttachmentOrigins,
    BrowserLocation,
    SourcePolicy,
)
from apps.oa.approvals import forms


def synthetic_policy():
    return SourcePolicy(
        AttachmentOrigins("https://oa.example.test", frozenset({"files.example.test"})),
        "https://trip.example.test",
        BrowserLocation(
            Path("/synthetic/chrome"),
            Path("/synthetic/profile"),
            "Default",
            "http://127.0.0.1:9223",
        ),
    )


def test_explicit_origin_policy_is_shared_by_form_and_download():
    policy = synthetic_policy()
    url = "https://files.example.test/opera/getFile/123/invoice.pdf"
    workflow = {
        "workflowMainTableInfo": {
            "requestRecords": [
                {
                    "workflowRequestTableFields": [
                        {
                            "fieldName": "attachment",
                            "fieldShowName": "附件",
                            "fieldValue": url,
                            "fieldShowValue": url,
                        },
                    ]
                }
            ]
        }
    }
    rows = forms.logical_attachment_resources(workflow, [], origins=policy.attachments)
    assert len(rows) == 1
    assert (
        policy.attachments.download_url(rows[0]["downloadUrl"], allow_extra=True) == url
    )
    assert (
        policy.attachments.permits_native_origin(
            "https://oa.example.test:444/news/download.do"
        )
        is False
    )
    assert policy.attachments.permits_native_origin("/news/download.do") is True


def test_policy_load_preserves_blank_process_values_and_captures_each_composition(
    tmp_path, monkeypatch
):
    from apps.oa.approvals.source_policy import load_source_policy
    from apps.oa.runtime import environment

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    configuration = tmp_path / "synthetic.env"
    configuration.write_text(
        "OA_BASE_URL=https://file.example.test\nOA_EXTRA_DOWNLOAD_HOSTS=files.example.test\n"
    )
    configuration.chmod(0o600)
    monkeypatch.setattr(environment, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(environment.load_local_env, "_loaded", False, raising=False)
    monkeypatch.setenv("OA_AUTH_ENV_FILE", str(configuration))
    monkeypatch.setenv("OA_BASE_URL", "")
    monkeypatch.delenv("OA_EXTRA_DOWNLOAD_HOSTS", raising=False)
    first = load_source_policy()
    monkeypatch.setenv("OA_BASE_URL", "https://second.example.test")
    second = load_source_policy()
    assert first.attachments.oa_origin == ""
    assert first.attachments.extra_hosts == frozenset({"files.example.test"})
    assert second.attachments.oa_origin == "https://second.example.test"


def test_source_imports_and_cli_help_do_not_load_private_configuration():
    import subprocess
    import sys

    script = """
import socket
from apps.oa.runtime import environment

def reject(*args, **kwargs):
    raise AssertionError('private_or_remote_access_forbidden')

environment.load_local_env = reject
socket.getaddrinfo = reject
socket.socket.connect = reject
from apps.oa.approvals import forms, operations
from apps.oa.approvals.sources import downloads, travel
from apps.oa.__main__ import main
for capability in ('approvals', 'materials', 'references'):
    try:
        main([capability, '--help'])
    except SystemExit as error:
        assert error.code == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_browser_cache_keeps_process_scope_and_invalidates_on_policy_change(
    monkeypatch,
):
    from dataclasses import replace
    from apps.oa.approvals.sources.downloads import DownloadTransfer
    from apps.oa.approvals.sources import browser_session

    calls = []
    policy = synthetic_policy()

    def capture(endpoint, **kwargs):
        calls.append((endpoint, kwargs["required_host"]))
        return {"cookies": [], "origins": []}

    monkeypatch.setattr(browser_session, "capture_storage_state", capture)
    first = DownloadTransfer(replace(policy, travel_origin="synthetic-cache-scope"))
    second = DownloadTransfer(first.policy)
    first.rpa_storage_state()
    second.rpa_storage_state()
    changed = replace(
        first.policy,
        attachments=replace(policy.attachments, oa_origin="https://other.example.test"),
    )
    DownloadTransfer(changed).rpa_storage_state()
    assert calls == [
        ("http://127.0.0.1:9223", "oa.example.test"),
        ("http://127.0.0.1:9223", "other.example.test"),
    ]
