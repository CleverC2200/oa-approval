from __future__ import annotations

import html
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Any
import urllib.parse

from apps.oa.materials import resources as attachments
from apps.oa.approvals import flows as flow


ATTACHMENT_FIELD_URL_RE = re.compile(r"https://[^\s\"'<>]+", re.IGNORECASE)
TRIP_ATTACHMENT_ID_RE = re.compile(
    r"/opera/(?:getFile|share/open)/(\d+)/",
    re.IGNORECASE,
)
_TRUSTED_OWNER_DETAIL_KEY = "_trusted_owner_detail_id"
_TRUSTED_SOURCE_FIELD_PATH_KEY = "_trusted_source_field_path"
_TRUSTED_SOURCE_FIELD_NAME_KEY = "_trusted_source_field_name"
_TRUSTED_SOURCE_REFERENCES_KEY = "_trusted_source_references"
_TRUSTED_ATTACHMENT_BINDING_KEYS = (
    _TRUSTED_OWNER_DETAIL_KEY, _TRUSTED_SOURCE_FIELD_PATH_KEY,
    _TRUSTED_SOURCE_FIELD_NAME_KEY, _TRUSTED_SOURCE_REFERENCES_KEY,
)
_COMPANY_NAME_COMPANION_FIELDS = {"bxgs": ("bxgsmc",)}


def _raw_field_is_business_value(field: dict[str, Any], value: str) -> bool:
    name = str(field.get("fieldShowName") or "").strip()
    code = str(field.get("fieldName") or "").strip()
    html_type = str(field.get("fieldHtmlType") or "")
    db_type = str(field.get("fieldDBType") or "")
    if name in {"发票号码", "关联发票"}:
        return False
    if html_type == "3" or db_type.startswith("browser."):
        if str(field.get("fieldType") or "") != "226":
            return False
        if (code, name, db_type) == ("sapkhbm", "SAP客户编码", "browser.6003"):
            return bool(re.fullmatch(r"[0-9]+", value))
        return (
            (code, name) == ("bxgs", "报销公司")
            and db_type in {"browser.5534", "browser.38009", "browser.19001"}
            and value.endswith("公司")
        )
    if html_type in {"1", "2"}:
        return True
    if html_type:
        return False
    return not re.fullmatch(r"[0-9,]+", value)


def _field_reading(field: dict[str, Any]) -> tuple[str, str, str]:
    display = flow.clean_field_text(field.get("fieldShowValue"))
    raw = flow.clean_field_text(field.get("fieldValue"))
    if display:
        return display, "display", "present"
    if raw and _raw_field_is_business_value(field, raw):
        return raw, "raw", "present"
    placeholder = any(
        marker in str(field.get("fieldShowValue") or "")
        for marker in ("[该字段暂不支持]", "[该字段仅支持显示]")
    )
    return "", "none", "unresolved" if raw or placeholder else "empty"


def _field_entry(
    field: dict[str, Any],
    *,
    table: str,
    row: int,
    path: str,
) -> dict[str, Any]:
    raw_value = field.get("fieldValue")
    value, value_source, read_status = _field_reading(field)
    return {
        "name": str(field.get("fieldShowName") or field.get("fieldName") or "").strip(),
        "field_name": str(field.get("fieldName") or "").strip(),
        "path": path,
        "value": value,
        "value_source": value_source,
        "read_status": read_status,
        "raw_value": "" if raw_value is None else str(raw_value),
        "table": table,
        "row": row,
        "attachment_html": str(field.get("filedHtmlShow") or ""),
    }


def _apply_company_name_companions(fields: list[dict[str, Any]]) -> None:
    for field in fields:
        if field.get("read_status") == "present":
            continue
        companions = _COMPANY_NAME_COMPANION_FIELDS.get(
            str(field.get("field_name") or "")
        )
        if not companions:
            continue
        for candidate in fields:
            if (
                str(candidate.get("field_name") or "") not in companions
                or str(candidate.get("table") or "") != str(field.get("table") or "")
                or str(candidate.get("row")) != str(field.get("row"))
                or candidate.get("read_status") != "present"
            ):
                continue
            value = str(candidate.get("value") or "").strip()
            if not value:
                continue
            field["value"] = value
            field["value_source"] = "companion"
            field["read_status"] = "present"
            break


def extract_fields(data: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    base = (
        data.get("workflowBaseInfo")
        if isinstance(data.get("workflowBaseInfo"), dict)
        else {}
    )
    main_records = (
        (data.get("workflowMainTableInfo") or {}).get("requestRecords")
        or (base.get("workflowMainTableInfo") or {}).get("requestRecords")
        or []
    )
    for row, record in enumerate(main_records):
        for field_index, field in enumerate(
            record.get("workflowRequestTableFields") or []
        ):
            if isinstance(field, dict):
                fields.append(
                    _field_entry(
                        field,
                        table="main",
                        row=row,
                        path=f"main[{row}].field[{field_index}]",
                    )
                )

    detail_tables = (
        data.get("workflowDetailTableInfos")
        or base.get("workflowDetailTableInfos")
        or []
    )
    for table_index, table in enumerate(detail_tables):
        for row, record in enumerate(table.get("workflowRequestTableRecords") or []):
            for field_index, field in enumerate(
                record.get("workflowRequestTableFields") or []
            ):
                if isinstance(field, dict):
                    fields.append(
                        _field_entry(
                            field,
                            table=f"detail_{table_index}",
                            row=row,
                            path=(
                                f"detail[{table_index}].row[{row}].field[{field_index}]"
                            ),
                        )
                    )
    _apply_company_name_companions(fields)
    return fields


def _resource_name_from_url(url: str) -> str:
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    return attachments.safe_filename(name, "attachment.bin")


def attachment_url_resources_from_fields(
    data: dict[str, Any], *, origins,
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field in extract_fields(data):
        if "附件" not in str(field.get("name") or ""):
            continue
        text = "\n".join(str(field.get(key) or "") for key in ("raw_value", "value"))
        for match in ATTACHMENT_FIELD_URL_RE.finditer(text):
            url = match.group(0).strip()
            parsed = urllib.parse.urlparse(url)
            if (
                not origins.permits_field_url(url)
                or url in seen
            ):
                continue
            seen.add(url)
            resources.append(
                {
                    "id": (
                        f"field_{field.get('table')}_{field.get('row')}_"
                        f"{len(resources) + 1}"
                    ),
                    "name": _resource_name_from_url(url),
                    "downloadUrl": url,
                    "allowExtraDownloadHost": True,
                    _TRUSTED_SOURCE_FIELD_PATH_KEY: str(field.get("path") or ""),
                    _TRUSTED_SOURCE_FIELD_NAME_KEY: str(field.get("name") or ""),
                }
            )
    return resources


def _trip_attachment_identity(resource: dict[str, Any], *, origins) -> str:
    raw_url = str(
        resource.get("downloadUrl") or resource.get("download_url") or ""
    ).strip()
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.hostname not in origins.extra_hosts:
        return ""
    match = TRIP_ATTACHMENT_ID_RE.search(parsed.path)
    return str(match.group(1)) if match else ""


class _AttachmentLinkParser(HTMLParser):

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        onclick = values.get("onclick") or ""
        match = re.fullmatch(r"\s*toURL\(['\"]([^'\"]+)['\"]\s*,\s*false\s*\);?\s*", onclick)
        if match:
            self.links.append(match.group(1))


def _validated_html_links(markup: str, *, origins) -> list[tuple[str, str]]:
    parser = _AttachmentLinkParser()
    parser.feed(markup)
    links = []
    for link in parser.links:
        try:
            parsed = urllib.parse.urlsplit(link)
            if parsed.path != "/news/download.do" or parsed.fragment:
                continue
            if not origins.permits_native_origin(link):
                continue
            query = urllib.parse.parse_qs(parsed.query)
        except ValueError:
            continue
        ids = query.get("fileid", [])
        if len(ids) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", ids[0]):
            continue
        filenames = query.get("filename", [f"attachment-{ids[0]}.bin"])
        if len(filenames) != 1 or not filenames[0]:
            continue
        links.append((ids[0], filenames[0]))
    return links


def _html_attachment_resources(data: dict[str, Any], *, origins) -> dict[str, dict[str, Any]]:
    sources = []
    for field in extract_fields(data):
        path = field["path"]
        owner = path.rpartition(".field[")[0] if field["table"].startswith("detail_") else None
        sources.append((path, field["name"], owner, field["attachment_html"]))
    for index, log in enumerate(data.get("workflowRequestLogs") or []):
        if isinstance(log, dict):
            sources.append((f"workflowRequestLogs[{index}].annexDocHtmls", "签字附件", None,
                            str(log.get("annexDocHtmls") or "")))
    rows: dict[str, dict[str, Any]] = {}
    for path, name, owner, markup in sources:
        reference = {"source_field_path": path, "source_field_name": name, "owner_detail_id": owner}
        for resource_id, filename in _validated_html_links(markup, origins=origins):
            rows.setdefault(resource_id, {
                "id": resource_id, "name": filename,
                "downloadUrl": "/weaver/weaver.file.FileDownload?" + urllib.parse.urlencode({"fileid": resource_id}),
                _TRUSTED_SOURCE_REFERENCES_KEY: [],
            })
            references = rows[resource_id][_TRUSTED_SOURCE_REFERENCES_KEY]
            if reference not in references:
                references.append(reference)
    for row in rows.values():
        references = row[_TRUSTED_SOURCE_REFERENCES_KEY]
        fields = [ref for ref in references if not ref["source_field_path"].startswith("workflowRequestLogs[")]
        primary = fields or references
        if len(primary) == 1:
            source = primary[0]
            row[_TRUSTED_SOURCE_FIELD_PATH_KEY] = source["source_field_path"]
            row[_TRUSTED_SOURCE_FIELD_NAME_KEY] = source["source_field_name"]
            if source["owner_detail_id"]:
                row[_TRUSTED_OWNER_DETAIL_KEY] = source["owner_detail_id"]
    return rows


def source_references(resource: dict[str, Any]) -> list[dict[str, str | None]]:
    if _TRUSTED_SOURCE_REFERENCES_KEY in resource:
        return resource[_TRUSTED_SOURCE_REFERENCES_KEY]
    if not (path := resource.get(_TRUSTED_SOURCE_FIELD_PATH_KEY)):
        return []
    return [{
        "source_field_path": path,
        "source_field_name": resource.get(_TRUSTED_SOURCE_FIELD_NAME_KEY),
        "owner_detail_id": resource.get(_TRUSTED_OWNER_DETAIL_KEY),
    }]


def logical_attachment_resources(
    data: dict[str, Any],
    resources: list[dict[str, Any]], *, origins,
) -> list[dict[str, Any]]:
    discovered = _html_attachment_resources(data, origins=origins)
    rows = []
    known_ids = set()
    for resource in attachments.attachment_resources(resources):
        item = dict(resource)
        resource_id = str(
            item.get("id") or item.get("resourceId") or item.get("fileId") or ""
        ).strip()
        attachment = discovered.get(resource_id, {})
        for key in _TRUSTED_ATTACHMENT_BINDING_KEYS:
            item.pop(key, None)
            if key in attachment:
                item[key] = attachment[key]
        rows.append(item)
        known_ids.add(resource_id)
    rows.extend(item for resource_id, item in discovered.items() if resource_id not in known_ids)
    known_urls = {
        str(item.get("downloadUrl") or item.get("download_url") or "").strip()
        for item in rows
    }
    for item in attachment_url_resources_from_fields(data, origins=origins):
        url = str(item.get("downloadUrl") or "").strip()
        if url and url not in known_urls:
            rows.append(item)
            known_urls.add(url)

    deduplicated: list[dict[str, Any]] = []
    seen_trip_ids: set[str] = set()
    for item in rows:
        trip_id = _trip_attachment_identity(item, origins=origins)
        if trip_id and trip_id in seen_trip_ids:
            continue
        if trip_id:
            seen_trip_ids.add(trip_id)
        deduplicated.append(item)
    return deduplicated
