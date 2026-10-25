from pathlib import Path
from types import SimpleNamespace

from apps.oa.approvals.source_policy import (
    AttachmentOrigins,
    BrowserLocation,
    SourcePolicy,
)


POLICY = SourcePolicy(
    AttachmentOrigins(
        "https://oa.example.test",
        frozenset({"files.example.test", "trip.example.test"}),
    ),
    "https://trip.example.test",
    BrowserLocation(
        Path("/synthetic/chrome"),
        Path("/synthetic/profile"),
        "Default",
        "http://127.0.0.1:9223",
    ),
)


def download_sources(direct, *, browser=None):
    from apps.oa.approvals.collection.downloads import DownloadSources

    def unexpected_browser(*_args, **_options):
        raise AssertionError("unexpected_browser_download")

    return DownloadSources(
        SimpleNamespace(
            direct=direct,
            browser=browser or unexpected_browser,
            normalize_download_url=lambda url,
            allow_extra_hosts=False: POLICY.attachments.download_url(
                url,
                allow_extra=allow_extra_hosts,
            ),
        ),
        None,
    )
