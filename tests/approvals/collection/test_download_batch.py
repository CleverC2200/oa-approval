from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.oa.approvals.collection.downloads import DownloadSources, download_resources
from apps.oa.approvals.sources.download_errors import (
    Downloaded,
    DownloadError,
    Failed,
    Unavailable,
)
from apps.oa.runtime import control
from tests.approvals.source_fakes import download_sources


def test_complete_download_distinguishes_unavailable_from_failure(tmp_path):
    calls = []

    class Transfer:
        def normalize_download_url(self, url, **_options):
            return url

        def direct(self, resource, **_options):
            calls.append(("direct", resource["id"]))
            return (
                Unavailable()
                if resource["id"] == "fallback"
                else Failed(DownloadError("attachment_download_failed", "下载超时"))
            )

        def browser(self, resource, *, dest_dir, **_options):
            calls.append(("browser", resource["id"]))
            path = dest_dir / "recorded.txt"
            path.write_text("recorded content")
            return Downloaded(path)

    result = download_resources(
        [{"id": name, "downloadUrl": "/" + name} for name in ("failed", "fallback")],
        sources=DownloadSources(Transfer(), None),
        dest_dir=tmp_path / "downloads",
        timeout_sec=1,
        allow_browser_download=True,
    )
    assert result.disposition == "partial"
    assert [item["resource_id"] for item in result.items] == ["fallback"]
    assert [(error.code, error.message) for error in result.errors] == [
        ("attachment_download_failed", "下载超时")
    ]
    assert set(calls[:2]) == {("direct", "failed"), ("direct", "fallback")}
    assert calls[2:] == [("browser", "fallback")]


@pytest.mark.parametrize(
    "gate, expected",
    [
        ("identity", "attachment_resource_identity_conflict"),
        ("authorization", "attachment_download_disabled"),
        ("stop", "oa_run_stopped"),
    ],
)
def test_complete_download_rejects_before_source_io(
    tmp_path, monkeypatch, gate, expected
):
    def forbidden(*_args, **_options):
        pytest.fail("source_io_before_download_admission")

    sources = download_sources(forbidden, browser=forbidden)
    sources = DownloadSources(sources.transfer, SimpleNamespace(snapshot=forbidden))
    targets = [
        {
            "id": "first",
            "downloadUrl": "/first",
            "_invoice_original": {"synthetic": "source"},
        }
    ]
    if gate == "identity":
        targets.append({**targets[0], "downloadUrl": "/second"})
    monkeypatch.setattr(control, "stop_requested", lambda: gate == "stop")
    result = download_resources(
        targets,
        sources=sources,
        dest_dir=tmp_path / "downloads",
        timeout_sec=1,
        allow_browser_download=gate != "authorization",
    )
    assert result.disposition == "blocked"
    assert result.items == ()
    assert result.errors[0].code == expected


def test_fatal_result_keeps_order_and_waits_for_all_direct_transfers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OA_ATTACHMENT_DOWNLOAD_CONCURRENCY", "1")
    calls = []

    def direct(resource, *, dest_dir, **_options):
        name = resource["id"]
        calls.append(("direct", name))
        if name == "ordinary":
            return Failed(
                DownloadError("attachment_download_failed", "ordinary failure")
            )
        if name == "fallback":
            return Unavailable()
        if name == "fatal":
            return Failed(
                DownloadError("oa_session_expired", "recorded session failure")
            )
        path = dest_dir / "later.txt"
        path.write_text("later evidence")
        return Downloaded(path)

    def browser(resource, *, dest_dir, **_options):
        calls.append(("browser", resource["id"]))
        path = dest_dir / "fallback.txt"
        path.write_text("fallback evidence")
        return Downloaded(path)

    names = ["ordinary", "fallback", "fatal", "later"]
    result = download_resources(
        [{"id": name, "downloadUrl": "/" + name} for name in names],
        sources=download_sources(direct, browser=browser),
        dest_dir=tmp_path / "downloads",
        timeout_sec=1,
        allow_browser_download=True,
    )
    assert result.disposition == "partial"
    assert [item["resource_id"] for item in result.items] == ["fallback"]
    assert [error.code for error in result.errors] == [
        "oa_session_expired",
        "attachment_download_failed",
    ]
    assert calls == [("direct", name) for name in names] + [("browser", "fallback")]
    assert (tmp_path / "downloads" / "later.txt").read_text() == "later evidence"


def test_cache_write_failure_does_not_change_download_result(tmp_path, monkeypatch):
    cache_directory = tmp_path / "not-a-sqlite-file"
    cache_directory.mkdir()
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", str(cache_directory))

    def direct(_resource, *, dest_dir, **_options):
        path = dest_dir / "recorded.txt"
        path.write_text("recorded evidence")
        return Downloaded(path)

    result = download_resources(
        [{"id": "one", "downloadUrl": "/one"}],
        sources=download_sources(direct),
        dest_dir=tmp_path / "downloads",
        timeout_sec=1,
        allow_browser_download=True,
    )
    assert result.disposition == "complete"
    assert result.errors == ()
    assert result.items[0]["resource_id"] == "one"
    assert (tmp_path / "downloads" / "recorded.txt").read_text() == "recorded evidence"
