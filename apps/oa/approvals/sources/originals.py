from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from apps.oa.approvals.sources import browser_session, native_documents
from apps.oa.approvals.sources.download_errors import (
    Downloaded,
    DownloadError,
    Failed,
    Unavailable,
    TransferOutcome,
)
from apps.oa.approvals.sources.downloads import direct_download
from apps.oa.materials.resources import deterministic_filename
from apps.oa.runtime import control, files


_MAX_ORIGINAL_BYTES = 50 * 1024 * 1024
_CLOUD_DOWNLOAD_URL = "https://api.mypiaojia.com/invoiceApi/file/download"


@dataclass(frozen=True, slots=True)
class OriginalContext:
    client: object
    sources: dict


class _NoCloudRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("attachment_download_failed")


class OriginalSources:
    def __init__(self, policy, transfer, *, read_context):
        self._policy = policy
        self._transfer = transfer
        self._read_context = read_context

    def snapshot(self, request_id: str) -> OriginalContext:
        return self._read_context(request_id)

    def _checked_resource(self, resource: dict) -> dict:
        url = self._policy.attachments.download_url(
            str(resource.get("downloadUrl") or resource.get("download_url") or ""),
            allow_extra=False,
        )
        return dict(
            resource,
            downloadUrl=url,
            _ephemeral_cookie_header=self._transfer._rpa_cookie_header(
                urllib.parse.urlsplit(self._policy.attachments.oa_origin).hostname or ""
            ),
        )

    def verify(
        self,
        context: OriginalContext,
        resource: dict,
        *,
        timeout_sec: int,
    ) -> bool:
        checked = self._checked_resource(resource)
        return self._verify(context, checked, timeout_sec=timeout_sec)

    def _verify(self, context, checked, *, timeout_sec, initialize=False) -> bool:
        source, _ = context.sources.get(str(checked.get("id") or ""), ({}, ""))
        if not source or source != checked.get("_invoice_original"):
            return False
        return native_documents.verify(
            checked,
            checked["downloadUrl"],
            base_url=self._policy.attachments.oa_origin,
            document_id=source["document_id"],
            timeout_sec=timeout_sec,
            initialize=initialize,
        )

    def read(
        self,
        context: OriginalContext,
        resource: dict,
        *,
        dest_dir: Path,
        timeout_sec: int,
    ) -> TransferOutcome:
        return direct_download(
            resource,
            timeout_sec=timeout_sec,
            operation=lambda: self._read(
                context, resource, dest_dir=dest_dir, timeout_sec=timeout_sec
            ),
        )

    def _read(self, context, resource, *, dest_dir, timeout_sec) -> TransferOutcome:
        checked = self._checked_resource(resource)
        url = checked["downloadUrl"]
        first = self._transfer.native(
            checked, url, dest_dir=dest_dir, timeout_sec=timeout_sec
        )
        if (
            not isinstance(first, Failed)
            or first.error.code != "attachment_access_denied"
        ):
            return first
        try:
            initialized = self._verify(
                context, checked, timeout_sec=timeout_sec, initialize=True
            )
        except browser_session.RpaSessionError as error:
            return Failed(DownloadError(error.code, f"{error.code}: {error}"))
        if not initialized:
            return first
        if control.stop_requested():
            return Failed(DownloadError(control.STOP_CODE, control.STOP_CODE))
        retried = self._transfer.native(
            checked, url, dest_dir=dest_dir, timeout_sec=timeout_sec
        )
        if (
            isinstance(retried, Failed)
            and retried.error.code == "attachment_access_denied"
        ):
            outcome = self._cloud(
                context,
                checked,
                dest=dest_dir / deterministic_filename(resource),
                timeout_sec=timeout_sec,
            )
            if isinstance(outcome, Failed):
                return Failed(
                    DownloadError(
                        outcome.error.code,
                        f"{outcome.error.code}: 关联发票原件读取失败",
                    )
                )
            return outcome
        return first if isinstance(retried, Unavailable) else retried

    def _cloud(self, context, resource, *, dest, timeout_sec) -> TransferOutcome:
        from apps.oa.approvals.sources import invoices

        request_id = str(resource.get("_workflow_request_id") or "")
        native_file_id = str(resource.get("id") or "")
        try:
            source, data_id = context.sources.get(native_file_id, ({}, ""))
            if not source or source != resource.get("_invoice_original"):
                return Failed(
                    DownloadError(
                        "resource_ownership_untrusted", "resource_ownership_untrusted"
                    )
                )
            response = context.client.get(invoices.TOKEN_PATH)
            token = response.get("token")
            if (
                str(response.get("status")) != "1"
                or not isinstance(token, str)
                or not token
            ):
                return Failed(
                    DownloadError(
                        "attachment_access_denied", "attachment_access_denied"
                    )
                )
            if control.stop_requested():
                return Failed(DownloadError(control.STOP_CODE, control.STOP_CODE))
            query = urllib.parse.urlencode(
                {
                    "id": source["cloud_file_id"],
                    "fid": source["cloud_record_id"],
                    "dataId": data_id,
                    "token": token,
                }
            )
            request = urllib.request.Request(
                _CLOUD_DOWNLOAD_URL + "?" + query,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://inc.mypiaojia.com/",
                },
            )
            with urllib.request.build_opener(_NoCloudRedirect()).open(
                request, timeout=max(1, timeout_sec)
            ) as response:
                length = response.headers.get("Content-Length")
                if length is not None and (
                    not length.isdecimal() or int(length) > _MAX_ORIGINAL_BYTES
                ):
                    return Failed(
                        DownloadError(
                            "attachment_content_incomplete",
                            "attachment_content_incomplete",
                        )
                    )
                body = response.read(_MAX_ORIGINAL_BYTES + 1)
                if (
                    len(body) > _MAX_ORIGINAL_BYTES
                    or not body.startswith(b"%PDF-")
                    or length is not None
                    and len(body) != int(length)
                ):
                    return Failed(
                        DownloadError(
                            "attachment_content_incomplete",
                            "attachment_content_incomplete",
                        )
                    )
            if not self._verify(context, resource, timeout_sec=timeout_sec):
                return Failed(
                    DownloadError(
                        "resource_ownership_untrusted", "resource_ownership_untrusted"
                    )
                )
            if control.stop_requested():
                return Failed(DownloadError(control.STOP_CODE, control.STOP_CODE))
            files.write_bytes(
                dest,
                body,
                source={
                    "request_id": request_id,
                    "resource_id": native_file_id,
                    "kind": "invoice_original",
                },
            )
            return Downloaded(dest)
        except urllib.error.HTTPError as error:
            code = (
                "attachment_access_denied"
                if error.code in (401, 403)
                else "attachment_download_failed"
            )
        except browser_session.RpaSessionError as error:
            code = error.code
        except ValueError as error:
            code = (
                str(error)
                if str(error).startswith("material_")
                else "attachment_download_failed"
            )
        except Exception:
            code = "attachment_download_failed"
        return Failed(DownloadError(code, code))
