from __future__ import annotations

import hashlib
from typing import Any, Dict, List
import urllib.parse


def resource_name(resource: Dict[str, Any]) -> str:
    return str(
        resource.get("name")
        or resource.get("fileName")
        or resource.get("filename")
        or ""
    ).strip()


def attachment_resources(resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item
        for item in resources
        if isinstance(item, dict)
        and str(item.get("downloadUrl") or item.get("download_url") or "").strip()
    ]


def _quote_download_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/%")
    query = urllib.parse.quote(parsed.query, safe="=&%?/:+,-._~")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, query, parsed.fragment)
    )


def safe_filename(name: str, fallback: str) -> str:
    text = str(name or "").strip().replace("/", "_").replace("\\", "_")
    return text or fallback


def resource_key(resource: Dict[str, Any]) -> str:
    raw_id = str(
        resource.get("id") or resource.get("resource_id") or resource.get("resourceId") or resource.get("fileId") or ""
    ).strip()
    if raw_id:
        return safe_filename(raw_id, "resource")
    raw_url = str(
        resource.get("downloadUrl") or resource.get("download_url") or ""
    ).strip()
    digest = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()[:12]
    return f"url_{digest}"


def deterministic_filename(resource: Dict[str, Any], downloaded_name: str = "") -> str:
    name = safe_filename(
        resource_name(resource) or downloaded_name or "attachment.bin", "attachment.bin"
    )
    key = safe_filename(resource_key(resource), "resource")
    return name if name.startswith(f"{key}_") else f"{key}_{name}"


def normalize_download_url(raw_url: str, *, oa_host: str = "", allowed_hosts=()) -> str:
    raw = str(raw_url or "").strip()
    if not raw:
        raise ValueError("downloadUrl is empty")
    if raw.startswith("//"):
        normalized = f"https:{raw}"
    elif raw.startswith(("http://", "https://")):
        normalized = raw
    else:
        normalized = f"{oa_host.rstrip('/')}{raw if raw.startswith('/') else '/' + raw}"
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme != "https":
        raise ValueError(f"downloadUrl scheme must be https: {parsed.scheme or '<empty>'}")
    if not parsed.hostname:
        raise ValueError("downloadUrl host is empty")
    if allowed_hosts and parsed.hostname not in allowed_hosts:
        raise ValueError(f"downloadUrl host not allowed: {parsed.hostname}")
    return normalized
