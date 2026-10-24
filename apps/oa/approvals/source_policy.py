from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import urllib.parse

from apps.oa.materials.resources import normalize_download_url
from apps.oa.runtime import environment


@dataclass(frozen=True, slots=True)
class AttachmentOrigins:
    oa_origin: str
    extra_hosts: frozenset[str]

    def download_url(self, raw_url: str, *, allow_extra: bool) -> str:
        hosts = {
            host for host in (urllib.parse.urlparse(self.oa_origin).hostname,) if host
        }
        if allow_extra:
            hosts.update(self.extra_hosts)
        return normalize_download_url(
            raw_url, oa_host=self.oa_origin, allowed_hosts=hosts
        )

    def permits_field_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme == "https" and parsed.hostname in self.extra_hosts

    def permits_native_origin(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.netloc and not parsed.scheme:
            return True
        origin = urllib.parse.urlsplit(self.oa_origin)
        return bool(
            parsed.scheme in ("", "https")
            and not parsed.username
            and not parsed.password
            and origin.hostname
            and parsed.hostname == origin.hostname
            and (parsed.port or 443) == (origin.port or 443)
        )


@dataclass(frozen=True, slots=True)
class BrowserLocation:
    chrome_binary: Path
    user_data_dir: Path
    profile_directory: str
    cdp_endpoint: str


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    attachments: AttachmentOrigins
    travel_origin: str
    browser: BrowserLocation


def load_source_policy() -> SourcePolicy:
    environment.load_local_env()
    return SourcePolicy(
        AttachmentOrigins(
            os.environ.get("OA_BASE_URL", "").strip().rstrip("/"),
            frozenset(
                host.strip().lower()
                for host in os.environ.get("OA_EXTRA_DOWNLOAD_HOSTS", "").split(",")
                if host.strip()
            ),
        ),
        os.environ.get("OA_TRIP_BASE_URL", "").rstrip("/"),
        BrowserLocation(
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path(
                os.environ.get(
                    "OA_CHROME_USER_DATA_DIR",
                    str(Path.home() / "Library/Application Support/OA RPA Chrome"),
                )
            ).expanduser(),
            os.environ.get("OA_CHROME_PROFILE_DIRECTORY", "Default"),
            "http://"
            + os.environ.get("OA_CHROME_REMOTE_DEBUGGING_ADDRESS", "127.0.0.1")
            + ":"
            + os.environ.get("OA_CHROME_REMOTE_DEBUGGING_PORT", "9223"),
        ),
    )
