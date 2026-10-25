from apps.oa.approvals.sources import travel_browser, downloads
from apps.oa.approvals.sources.travel_evidence import CandidateObservation


def observation(record):
    return CandidateObservation(
        text=record.get("text", ""),
        download_targets=tuple(record.get("resources", [])),
        scan_status=record.get("attachment_scan_status", ""),
        inventory=tuple(record.get("attachment_inventory", [])),
        hotel_details=tuple(record.get("hotel_order_details", [])),
        browser=record.get("browser", ""),
        diagnostics=tuple(record.get("diagnostics", [])),
    )


def recorded_observations(monkeypatch, read):
    def read_attempt(*args, **kwargs):
        return [observation(record) for record in read(*args, **kwargs)]

    monkeypatch.setattr(
        travel_browser.BrowserAdapter, "read_attempt", staticmethod(read_attempt)
    )
    monkeypatch.setattr(
        downloads.DownloadTransfer,
        "rpa_storage_state",
        staticmethod(lambda **_kwargs: {"cookies": [], "origins": []}),
    )
    monkeypatch.setattr(downloads, "browser_visible_fallback_enabled", lambda: False)
