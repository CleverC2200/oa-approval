from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
import json
import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Literal, Protocol, Tuple

from apps.oa.approvals.collection import cache as attachment_cache
from apps.oa.approvals.sources import downloads as remote
from apps.oa.approvals.sources import browser_session as rpa_session
from apps.oa.approvals.sources.download_errors import (
    Downloaded,
    DownloadError,
    Failed,
    Unavailable,
    TransferOutcome,
)
from apps.oa.approvals.sources.originals import OriginalContext
from apps.oa.materials import archives
from apps.oa.materials.resources import (
    attachment_resources,
    resource_name,
    resource_key,
)
from apps.oa.runtime import control as runtime_control


MANIFEST_NAME = "manifest.json"
ATTACHMENT_IDENTITY_CONFLICT_CODE = "attachment_resource_identity_conflict"
DEFAULT_DOWNLOAD_CONCURRENCY = 4
MAX_DOWNLOAD_CONCURRENCY = 8


def _unique_attachment_resources(
    resources: List[Dict[str, Any]],
    *,
    transfer,
) -> tuple[list[dict], tuple[DownloadError, ...]]:
    unique: List[Dict[str, Any]] = []
    identities: Dict[str, str] = {}
    for resource in attachment_resources(resources):
        key = resource_key(resource)
        raw_url = str(
            resource.get("downloadUrl") or resource.get("download_url") or ""
        ).strip()
        try:
            identity = transfer.normalize_download_url(
                raw_url,
                allow_extra_hosts=bool(resource.get("allowExtraDownloadHost")),
            )
        except ValueError:
            identity = raw_url
        previous = identities.get(key)
        if previous is None:
            identities[key] = identity
            unique.append(resource)
            continue
        if previous != identity:
            return [], (
                DownloadError(
                    ATTACHMENT_IDENTITY_CONFLICT_CODE,
                    f"资源 {key} 对应多个下载地址，无法确认附件身份",
                ),
            )
    return unique, ()


def _download_worker_count(pending_count: int, raw_concurrency: str) -> int:
    if pending_count < 1:
        return 0
    try:
        configured = int(str(raw_concurrency or DEFAULT_DOWNLOAD_CONCURRENCY))
    except Exception:
        configured = DEFAULT_DOWNLOAD_CONCURRENCY
    return min(max(1, configured), MAX_DOWNLOAD_CONCURRENCY, pending_count)


def _manifest_path(dest_dir: Path) -> Path:
    return dest_dir / MANIFEST_NAME


def _load_manifest(dest_dir: Path) -> Dict[str, Any]:
    path = _manifest_path(dest_dir)
    if not path.exists():
        return {"version": 1, "resources": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "resources": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "resources": {}}
    payload.setdefault("version", 1)
    if not isinstance(payload.get("resources"), dict):
        payload["resources"] = {}
    return payload


def _remove_legacy_manifest(dest_dir: Path) -> None:
    if not attachment_cache.is_enabled():
        return
    path = _manifest_path(dest_dir)
    if not path.exists():
        return
    try:
        path.unlink()
    except Exception:
        pass


def _stored_path(dest_dir: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(dest_dir.resolve()))
    except Exception:
        return str(path)


def _resolve_stored_path(dest_dir: Path, raw_path: Any) -> Path:
    text = str(raw_path or "").strip()
    path = Path(text)
    return path if path.is_absolute() else dest_dir / path


def _manifest_entry_to_item(
    dest_dir: Path, resource: Dict[str, Any], entry: Dict[str, Any]
) -> Dict[str, Any] | None:
    path = _resolve_stored_path(dest_dir, entry.get("path"))
    if not remote.valid_attachment_file(path):
        return None
    extracted_files = []
    for child in (
        entry.get("extracted_files")
        if isinstance(entry.get("extracted_files"), list)
        else []
    ):
        if not isinstance(child, dict):
            continue
        child_path = _resolve_stored_path(dest_dir, child.get("path"))
        if not remote.valid_attachment_file(child_path):
            return None
        next_child = dict(child)
        next_child["path"] = str(child_path)
        extracted_files.append(next_child)
    item = {
        "resource_id": str(resource.get("id") or entry.get("resource_id") or ""),
        "name": resource_name(resource) or str(entry.get("name") or path.name),
        "path": str(path),
        "reused": True,
    }
    if entry.get("archive_status"):
        item["archive_status"] = entry.get("archive_status")
        item["archive_extracted_count"] = int(
            entry.get("archive_extracted_count") or len(extracted_files)
        )
        item["extracted_files"] = extracted_files
    return item


def _item_to_manifest_entry(dest_dir: Path, item: Dict[str, Any]) -> Dict[str, Any]:
    entry = {
        "resource_id": str(item.get("resource_id") or ""),
        "name": str(item.get("name") or ""),
        "path": _stored_path(dest_dir, Path(str(item.get("path") or ""))),
        "updated_at": int(time.time()),
    }
    if item.get("archive_status"):
        children = []
        for child in (
            item.get("extracted_files")
            if isinstance(item.get("extracted_files"), list)
            else []
        ):
            next_child = dict(child)
            next_child["path"] = _stored_path(
                dest_dir, Path(str(child.get("path") or ""))
            )
            children.append(next_child)
        entry["archive_status"] = item.get("archive_status")
        entry["archive_extracted_count"] = int(
            item.get("archive_extracted_count") or len(children)
        )
        entry["extracted_files"] = children
    return entry


def _cache_scope(
    dest_dir: Path, *, request_id: str = "", flow_key: str = ""
) -> Tuple[str, str]:
    resolved_request_id = str(request_id or dest_dir.name or "").strip()
    resolved_flow_key = str(flow_key or dest_dir.parent.name or "").strip()
    return resolved_request_id, resolved_flow_key


def _cached_attachment_entry(
    dest_dir: Path,
    resource: Dict[str, Any],
    *,
    request_id: str,
    flow_key: str,
    legacy_resources: Dict[str, Any],
    migrate_legacy: bool = True,
) -> Dict[str, Any] | None:
    key = resource_key(resource)
    cached_entry = attachment_cache.latest_attachment_file(request_id, flow_key, key)
    legacy_entry = (
        legacy_resources.get(key)
        if isinstance(legacy_resources.get(key), dict)
        else None
    )
    if cached_entry:
        return cached_entry
    if legacy_entry:
        if migrate_legacy:
            attachment_cache.record_attachment_file(
                request_id=request_id,
                flow_key=flow_key,
                resource_key=key,
                entry_json=legacy_entry,
            )
        return legacy_entry
    return None


def _attachment_content_sha256(path: Path) -> str:
    try:
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except OSError:
        return ""


def _record_attachment_entry(
    dest_dir: Path,
    resource: Dict[str, Any],
    item: Dict[str, Any],
    *,
    request_id: str,
    flow_key: str,
    metadata: Dict[str, Any] | None = None,
) -> None:
    entry = _item_to_manifest_entry(dest_dir, item)
    if metadata is not None:
        entry.update(metadata)
    attachment_cache.record_attachment_file(
        request_id=request_id,
        flow_key=flow_key,
        resource_key=resource_key(resource),
        entry_json=entry,
    )


@dataclass(frozen=True, slots=True)
class DownloadSources:
    transfer: ResourceTransfer
    originals: OriginalSource | None


class ResourceTransfer(Protocol):
    def normalize_download_url(
        self, raw_url: str, *, allow_extra_hosts: bool = False
    ) -> str: ...

    def direct(
        self, resource: dict, *, dest_dir: Path, timeout_sec: int
    ) -> TransferOutcome: ...

    def browser(
        self, resource: dict, *, dest_dir: Path, timeout_sec: int
    ) -> TransferOutcome: ...


class OriginalSource(Protocol):
    def snapshot(self, request_id: str) -> OriginalContext: ...

    def verify(
        self, context: OriginalContext, resource: dict, *, timeout_sec: int
    ) -> bool: ...

    def read(
        self,
        context: OriginalContext,
        resource: dict,
        *,
        dest_dir: Path,
        timeout_sec: int,
    ) -> TransferOutcome: ...


@dataclass(frozen=True, slots=True)
class DownloadResult:
    items: tuple[dict, ...]
    errors: tuple[DownloadError, ...]
    disposition: Literal["complete", "partial", "blocked"]


@dataclass(frozen=True, slots=True)
class _Cached:
    item: dict


def build_download_sources(source_policy) -> DownloadSources:
    from apps.oa.approvals.collection.invoice_originals import (
        read_original_context,
        initialize_workflow_document,
    )
    from apps.oa.approvals.sources.originals import OriginalSources

    transfer = remote.DownloadTransfer(
        source_policy,
        initialize_context=partial(
            initialize_workflow_document, source_policy=source_policy
        ),
    )
    return DownloadSources(
        transfer,
        OriginalSources(
            source_policy,
            transfer,
            read_context=partial(read_original_context, source_policy=source_policy),
        ),
    )


class _ContentPool:
    def __init__(self, dest_dir: Path):
        self.dest_dir = dest_dir
        self.existing_paths = frozenset(dest_dir.iterdir())
        self.paths = {}
        self.lock = threading.Lock()

    def share(self, path: Path, *, newly_downloaded: bool = False) -> Path:
        with self.lock:
            digest = _attachment_content_sha256(path)
            if not digest:
                raise RuntimeError("attachment_content_unreadable")
            key = (path.suffix.lower(), digest)
            previous = self.paths.get(key)
            if previous is None:
                self.paths[key] = path
                return path
            if (
                newly_downloaded
                and path != previous
                and path not in self.existing_paths
                and path.parent.resolve() == self.dest_dir.resolve()
                and not path.is_symlink()
            ):
                path.unlink()
            return previous


def _stop_error() -> DownloadError:
    return DownloadError(
        runtime_control.STOP_CODE, "已请求停止财务预审，停止本单附件下载"
    )


class _DownloadBatch:
    def __init__(
        self,
        targets,
        *,
        sources,
        dest_dir,
        timeout_sec,
        request_id,
        flow_key,
        reuse_cache,
    ):
        self.targets = targets
        self.sources = sources
        self.dest_dir = dest_dir
        self.timeout_sec = timeout_sec
        self.request_id = request_id
        self.cache_request_id, self.cache_flow_key = _cache_scope(
            dest_dir, request_id=request_id, flow_key=flow_key
        )
        self.reuse_cache = reuse_cache
        self.content = _ContentPool(dest_dir)
        self.legacy_resources = _load_manifest(dest_dir).setdefault("resources", {})
        self.original_context = None
        self.outcomes = [Unavailable() for _ in targets]
        self.items = []
        self.errors = []
        self.cache_entries = []

    def run(self) -> DownloadResult:
        if runtime_control.stop_requested():
            return DownloadResult((), (_stop_error(),), "blocked")
        if any(resource.get("_invoice_original") for resource in self.targets):
            errors = self._check_originals()
            if errors:
                return DownloadResult((), errors, "blocked")
        self._obtain()
        self._materialize()
        if self.original_context is not None:
            errors = self._check_terminal_sources()
            if errors:
                return DownloadResult((), errors, "blocked")
        self._publish_cache()
        _remove_legacy_manifest(self.dest_dir)
        return DownloadResult(
            tuple(self.items),
            tuple(self.errors),
            "partial" if self.errors else "complete",
        )

    def _check_originals(self) -> tuple[DownloadError, ...]:
        try:
            self.original_context = self.sources.originals.snapshot(self.request_id)
        except rpa_session.RpaSessionError as error:
            return (DownloadError(error.code, "发票原件会话核验失败"),)
        except Exception:
            return (
                DownloadError("resource_ownership_untrusted", "发票原件归属核验失败"),
            )
        for resource in self.targets:
            if not resource.get("_invoice_original"):
                continue
            try:
                valid = self.sources.originals.verify(
                    self.original_context, resource, timeout_sec=self.timeout_sec
                )
            except rpa_session.RpaSessionError as error:
                return (DownloadError(error.code, "发票原件会话核验失败"),)
            except ValueError:
                valid = False
            if not valid:
                return (
                    DownloadError(
                        "resource_ownership_untrusted", "发票原件归属核验失败"
                    ),
                )
        return ()

    def _check_terminal_sources(self) -> tuple[DownloadError, ...]:
        if runtime_control.stop_requested():
            return (_stop_error(),)
        try:
            current = self.sources.originals.snapshot(self.request_id)
            valid = (
                bool(self.original_context.sources)
                and current.sources == self.original_context.sources
            )
        except rpa_session.RpaSessionError as error:
            return (DownloadError(error.code, "发票原件会话核验失败"),)
        except Exception:
            valid = False
        if runtime_control.stop_requested():
            return (_stop_error(),)
        return (
            ()
            if valid
            else (
                DownloadError(
                    "resource_ownership_untrusted", "发票原件批量下载归属已变化"
                ),
            )
        )

    def _cached(self, resource) -> dict | None:
        if not self.reuse_cache:
            return None
        entry = _cached_attachment_entry(
            self.dest_dir,
            resource,
            request_id=self.cache_request_id,
            flow_key=self.cache_flow_key,
            legacy_resources=self.legacy_resources,
            migrate_legacy=self.original_context is None,
        )
        if entry and resource.get("_invoice_original"):
            path = _resolve_stored_path(self.dest_dir, entry.get("path"))
            if (
                entry.get("invoice_original_source") != resource["_invoice_original"]
                or not entry.get("content_sha256")
                or entry["content_sha256"] != _attachment_content_sha256(path)
            ):
                return None
        return (
            _manifest_entry_to_item(self.dest_dir, resource, entry) if entry else None
        )

    def _direct(self, resource) -> TransferOutcome:
        if resource.get("_invoice_original") and self.original_context is not None:
            outcome = self.sources.originals.read(
                self.original_context,
                resource,
                dest_dir=self.dest_dir,
                timeout_sec=self.timeout_sec,
            )
        else:
            outcome = self.sources.transfer.direct(
                resource, dest_dir=self.dest_dir, timeout_sec=self.timeout_sec
            )
        if isinstance(outcome, Downloaded):
            return Downloaded(self.content.share(outcome.path, newly_downloaded=True))
        return outcome

    def _obtain(self):
        pending = []
        for index, resource in enumerate(self.targets):
            cached = self._cached(resource)
            if cached:
                cached["path"] = str(self.content.share(Path(cached["path"])))
                self.outcomes[index] = _Cached(cached)
            else:
                pending.append((index, resource))
        raw_concurrency = str(
            os.environ.get("OA_ATTACHMENT_DOWNLOAD_CONCURRENCY") or ""
        ).strip()
        if not pending:
            return
        with ThreadPoolExecutor(
            max_workers=_download_worker_count(len(pending), raw_concurrency)
        ) as executor:
            futures = [
                (index, executor.submit(copy_context().run, self._direct, resource))
                for index, resource in pending
            ]
            for index, future in futures:
                try:
                    self.outcomes[index] = future.result()
                except Exception as error:
                    self.outcomes[index] = Failed(
                        DownloadError(
                            "attachment_download_failed",
                            f"{resource_name(self.targets[index]) or 'attachment.bin'}: 附件下载失败: {type(error).__name__}",
                        )
                    )

    def _expanded(self, resource, item, *, cached):
        item, error = archives._attach_archive_children(self.dest_dir, resource, item)
        if error:
            code = (
                "attachment_download_failed" if cached else "attachment_archive_failed"
            )
            return Failed(DownloadError(code, error))
        return item

    def _materialize_one(self, resource, outcome):
        if isinstance(outcome, _Cached):
            return self._expanded(resource, outcome.item, cached=True)
        if isinstance(outcome, Unavailable):
            outcome = self.sources.transfer.browser(
                resource, dest_dir=self.dest_dir, timeout_sec=self.timeout_sec
            )
            if isinstance(outcome, Downloaded):
                outcome = Downloaded(
                    self.content.share(outcome.path, newly_downloaded=True)
                )
        if not isinstance(outcome, Downloaded):
            return outcome
        path = outcome.path
        if not remote.valid_attachment_file(path):
            code = remote._file_download_fail_code(path) or "attachment_download_failed"
            return Failed(
                DownloadError(
                    code, f"{resource_name(resource) or path.name}: 下载文件无效"
                )
            )
        item = {
            "resource_id": str(resource.get("id") or ""),
            "name": resource_name(resource) or path.name,
            "path": str(path),
            "reused": False,
        }
        return self._expanded(resource, item, cached=False)

    def _materialize(self):
        for resource, outcome in zip(self.targets, self.outcomes):
            item = self._materialize_one(resource, outcome)
            if isinstance(item, Failed):
                self.errors.append(item.error)
                if item.error.code in remote.FATAL_DOWNLOAD_ERROR_CODES:
                    self.errors.insert(0, self.errors.pop())
                    break
            elif isinstance(item, dict):
                self.items.append(item)
                self.cache_entries.append((resource, item))

    def _publish_cache(self):
        for resource, item in self.cache_entries:
            metadata = {}
            if resource.get("_invoice_original"):
                digest = _attachment_content_sha256(Path(item["path"]))
                if not digest:
                    continue
                metadata = {
                    "invoice_original_source": dict(resource["_invoice_original"]),
                    "content_sha256": digest,
                }
            _record_attachment_entry(
                self.dest_dir,
                resource,
                item,
                request_id=self.cache_request_id,
                flow_key=self.cache_flow_key,
                metadata=metadata,
            )


def download_resources(
    resources: List[Dict[str, Any]],
    *,
    sources: DownloadSources,
    dest_dir: Path,
    timeout_sec: int,
    allow_browser_download: bool,
    request_id: str = "",
    flow_key: str = "",
    reuse_cache: bool = False,
) -> DownloadResult:
    targets, errors = _unique_attachment_resources(resources, transfer=sources.transfer)
    if errors:
        return DownloadResult((), errors, "blocked")
    targets = [
        dict(resource, _workflow_request_id=str(request_id).strip())
        for resource in targets
    ]
    if not targets:
        return DownloadResult((), (), "complete")
    if not allow_browser_download and not remote.is_enabled(
        os.environ.get("OA_ALLOW_BROWSER_DOWNLOAD")
    ):
        return DownloadResult(
            (),
            (
                DownloadError(
                    "attachment_download_disabled",
                    "附件下载未授权，需传 --allow-browser-download",
                ),
            ),
            "blocked",
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    return _DownloadBatch(
        targets,
        sources=sources,
        dest_dir=dest_dir,
        timeout_sec=timeout_sec,
        request_id=str(request_id).strip(),
        flow_key=flow_key,
        reuse_cache=reuse_cache,
    ).run()
