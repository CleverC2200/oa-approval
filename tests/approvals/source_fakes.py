from pathlib import Path

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
