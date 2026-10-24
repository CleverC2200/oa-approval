from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Tuple
import urllib.parse

from apps.oa.approvals.collection import cache as attachment_cache
from apps.oa.approvals.sources import downloads as remote
from apps.oa.approvals.sources import browser_session as rpa_session
from apps.oa.approvals.sources.download_errors import ATTACHMENT_ACCESS_DENIED_CODE
from apps.oa.materials import archives
from apps.oa.materials.resources import attachment_resources, resource_name, resource_key
from apps.oa.runtime import slots as resource_slots
from apps.oa.runtime import control as runtime_control


MANIFEST_NAME = "manifest.json"
ATTACHMENT_IDENTITY_CONFLICT_CODE = "attachment_resource_identity_conflict"
DEFAULT_DOWNLOAD_CONCURRENCY = 4
MAX_DOWNLOAD_CONCURRENCY = 8


def _unique_attachment_resources(
    resources: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    unique: List[Dict[str, Any]] = []
    identities: Dict[str, str] = {}
    for resource in attachment_resources(resources):
        key = resource_key(resource)
        raw_url = str(
            resource.get("downloadUrl") or resource.get("download_url") or ""
        ).strip()
        try:
            identity = remote.normalize_download_url(
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
            return [], [
                {
                    "code": ATTACHMENT_IDENTITY_CONFLICT_CODE,
                    "message": f"资源 {key} 对应多个下载地址，无法确认附件身份",
                }
            ]
    return unique, []


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
        with path.open('rb') as source:
            return hashlib.file_digest(source, 'sha256').hexdigest()
    except OSError:
        return ''


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


def _initialize_workflow_document_context(
    resource: Dict[str, Any], url: str, *, timeout_sec: int,
) -> bool:
    from apps.oa.approvals.collection import invoice_originals
    from apps.oa.approvals.sources import native_documents
    from apps.oa.approvals.sources import official_sql

    if not native_documents.accepts(resource, url, base_url=remote.OA_HOST):
        return False
    request_id = str(resource.get('_workflow_request_id') or '')
    file_id = str(resource.get('id') or '')
    try:
        document_id = official_sql.workflow_attachment_document(request_id, file_id)
        if not document_id:
            document_id = invoice_originals.source_for_download(request_id, file_id).get('document_id', '')
        return native_documents.verify(
            resource, url, base_url=remote.OA_HOST, document_id=document_id,
            timeout_sec=timeout_sec,
        )
    except rpa_session.RpaSessionError:
        raise
    except Exception:
        return False


def _download_resource_direct(
    resource: Dict[str, Any], *, dest_dir: Path, timeout_sec: int, original_batch=None,
) -> Tuple[Path | None, str]:
    name = resource_name(resource) or "attachment.bin"
    try:
        with resource_slots.acquire(
            "attachment-direct",
            capacity=remote._global_download_capacity(),
            timeout_sec=remote._resource_slot_wait_timeout(timeout_sec),
        ):
            if original_batch is not None and resource.get('_invoice_original'):
                return original_batch.download(resource, dest_dir=dest_dir, timeout_sec=timeout_sec)
            return remote._download_resource_direct_uncoordinated(
                resource,
                dest_dir=dest_dir,
                timeout_sec=timeout_sec,
                initialize_context=_initialize_workflow_document_context,
            )
    except resource_slots.ResourceSlotTimeout:
        return None, (
            f"{remote.RESOURCE_SLOT_TIMEOUT_CODE}: {name}: 等待全局附件下载通道超时"
        )


def download_resources(
    resources: List[Dict[str, Any]],
    *,
    dest_dir: Path,
    timeout_sec: int,
    allow_browser_download: bool,
    request_id: str = "",
    flow_key: str = "",
    reuse_cache: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    targets, identity_errors = _unique_attachment_resources(resources)
    if identity_errors:
        return [], identity_errors
    targets = [dict(resource, _workflow_request_id=str(request_id).strip()) for resource in targets]
    if not targets:
        return [], []
    if not allow_browser_download and not remote.is_enabled(
        os.environ.get("OA_ALLOW_BROWSER_DOWNLOAD")
    ):
        return [], [
            {
                "code": "attachment_download_disabled",
                "message": "附件下载未授权，需传 --allow-browser-download",
            }
        ]

    downloaded: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing_paths = frozenset(dest_dir.iterdir())
    cache_request_id, cache_flow_key = _cache_scope(
        dest_dir, request_id=request_id, flow_key=flow_key
    )
    legacy_manifest = _load_manifest(dest_dir)
    legacy_resources = legacy_manifest.setdefault("resources", {})
    if runtime_control.stop_requested():
        return [], [
            runtime_control.stop_error("已请求停止财务预审，停止本单附件下载")
        ]

    content_paths: dict[tuple[str, str], Path] = {}
    content_lock = threading.Lock()

    def share_content(path: Path, *, newly_downloaded: bool = False) -> Path:
        with content_lock:
            digest = _attachment_content_sha256(path)
            if not digest:
                raise RuntimeError("attachment_content_unreadable")
            key = (path.suffix.lower(), digest)
            previous = content_paths.get(key)
            if previous is None:
                content_paths[key] = path
                return path
            if (newly_downloaded and path != previous and path not in existing_paths
                    and path.parent.resolve() == dest_dir.resolve() and not path.is_symlink()):
                path.unlink()
            return previous

    def download(resource: Dict[str, Any]) -> Tuple[Path | None, str]:
        path, error = _download_resource_direct(
            resource, dest_dir=dest_dir, timeout_sec=timeout_sec, original_batch=original_batch,
        )
        if path is not None and not error:
            path = share_content(path, newly_downloaded=True)
        return path, error

    outcomes: List[Dict[str, Any]] = [{} for _item in targets]
    pending: List[tuple[int, Dict[str, Any]]] = []
    original_batch = None
    if any(resource.get('_invoice_original') for resource in targets):
        from apps.oa.approvals.collection.invoice_originals import DownloadBatch

        try:
            original_batch = DownloadBatch(
                str(request_id).strip(), base_url=remote.OA_HOST, cookie_header=remote._rpa_cookie_header,
                native_read=remote._direct_session_cookie_download,
            )
        except rpa_session.RpaSessionError as error:
            return [], [{'code': error.code, 'message': '发票原件会话核验失败'}]
        except Exception:
            return [], [{'code': 'resource_ownership_untrusted', 'message': '发票原件归属核验失败'}]
        errors = original_batch.prepare(targets, timeout_sec=timeout_sec)
        if errors:
            return [], errors
    for index, resource in enumerate(targets):
        cached_entry = _cached_attachment_entry(
            dest_dir,
            resource,
            request_id=cache_request_id,
            flow_key=cache_flow_key,
            legacy_resources=legacy_resources,
            migrate_legacy=original_batch is None,
        ) if reuse_cache else None
        if cached_entry and original_batch is not None:
            cached_entry = original_batch.accept_cache(
                resource, cached_entry, _resolve_stored_path(dest_dir, cached_entry.get('path')),
            )
        if cached_entry:
            cached_item = _manifest_entry_to_item(dest_dir, resource, cached_entry)
            if cached_item:
                cached_item["path"] = str(share_content(Path(cached_item["path"])))
                outcomes[index] = {"item": cached_item}
                continue
        pending.append((index, resource))

    raw_concurrency = str(
        os.environ.get("OA_ATTACHMENT_DOWNLOAD_CONCURRENCY") or ""
    ).strip()
    worker_count = _download_worker_count(len(pending), raw_concurrency)
    if pending:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                (
                    index,
                    executor.submit(
                        copy_context().run,
                        download,
                        resource,
                    ),
                )
                for index, resource in pending
            ]
            for index, future in futures:
                try:
                    path, err = future.result()
                except Exception as error:
                    path, err = None, (
                        f"{resource_name(targets[index]) or 'attachment.bin'}: "
                        f"附件下载失败: {type(error).__name__}"
                    )
                outcomes[index] = {"path": path, "error": err}

    cache_entries = []
    for index, resource in enumerate(targets):
        outcome = outcomes[index]
        cached_item = outcome.get("item")
        if isinstance(cached_item, dict):
            item, archive_err = archives._attach_archive_children(
                dest_dir, resource, cached_item
            )
            path = None
            err = archive_err
        else:
            path = outcome.get("path")
            err = str(outcome.get("error") or "")
            if path is None and not err:
                path, err = remote._download_resource_with_browser(
                    resource,
                    dest_dir=dest_dir,
                    timeout_sec=timeout_sec,
                )
                if path is not None and not err:
                    path = share_content(path, newly_downloaded=True)
            item = None
        if err:
            code = remote.download_error_code(err)
            errors.append({"code": code, "message": err})
            if code in remote.FATAL_DOWNLOAD_ERROR_CODES:
                errors.insert(0, errors.pop())
                break
            continue
        if item is None and path is not None:
            if not remote.valid_attachment_file(path):
                code = remote._file_download_fail_code(path) or "attachment_download_failed"
                errors.append(
                    {
                        "code": code,
                        "message": f"{resource_name(resource) or path.name}: 下载文件无效",
                    }
                )
                if code in remote.FATAL_DOWNLOAD_ERROR_CODES:
                    errors.insert(0, errors.pop())
                    break
                continue
            item = {
                "resource_id": str(resource.get("id") or ""),
                "name": resource_name(resource) or path.name,
                "path": str(path),
                "reused": False,
            }
            item, archive_err = archives._attach_archive_children(dest_dir, resource, item)
            if archive_err:
                errors.append(
                    {"code": "attachment_archive_failed", "message": archive_err}
                )
                continue
        if item is not None:
            downloaded.append(item)
            cache_entries.append((resource, item))
    if original_batch is not None:
        errors_after_download = original_batch.finish()
        if errors_after_download:
            return [], errors_after_download
    for resource, item in cache_entries:
        metadata = original_batch.cache_metadata(resource, Path(item['path'])) if original_batch else {}
        if metadata is not None:
            _record_attachment_entry(
                dest_dir, resource, item,
                request_id=cache_request_id, flow_key=cache_flow_key, metadata=metadata,
            )
    _remove_legacy_manifest(dest_dir)
    return downloaded, errors

