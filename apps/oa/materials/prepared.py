from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Literal


SUCCESSFUL_PREPARATION_STATUSES = {
    'archive_extracted', 'docx_read', 'image', 'image_converted',
    'legacy_spreadsheet_read', 'ofd_invoice_read', 'pdf_rendered',
    'spreadsheet_read', 'video_rendered', 'xml_invoice_read',
}
SKIPPED_PREPARATION_STATUSES = {'video_skipped'}


def skipped_video_sources(manifest: list[dict]) -> list[dict]:
    return [source for entry in manifest for source in (
        [entry] if entry.get('status') == 'video_skipped' else entry.get('skipped_videos', [])
    )]


def _valid_digest(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


@dataclass(frozen=True, slots=True)
class ResourceCoverage:
    resource_id: str
    manifest_count: int
    status: str | None
    declared_image_count: int | None
    declared_page_count: int | None
    direct_image_count: int
    related_image_count: int
    direct_source_sha256: str | None


class PreparedMaterial:
    __slots__ = ('_record',)

    def __init__(self, record: dict):
        self._record = deepcopy(record)

    def to_record(self) -> dict:
        return deepcopy(self._record)

    def coverage(self, resource_id: str) -> ResourceCoverage | None:
        manifest = [entry for entry in self._record.get('manifest', [])
                    if entry.get('resource_id') == resource_id]
        related = [page for page in self._record.get('images', [])
                   if resource_id in {page.get('resource_id'), page.get('parent_resource_id')}]
        direct = [page for page in related if page.get('resource_id') == resource_id]
        if not manifest and not related:
            return None
        entry = manifest[0] if len(manifest) == 1 else {}
        hashes = [page.get('source_content_sha256') for page in direct]
        digest = hashes[0] if hashes and all(_valid_digest(value) and value == hashes[0]
                                            for value in hashes) else None
        return ResourceCoverage(
            resource_id=resource_id, manifest_count=len(manifest), status=entry.get('status'),
            declared_image_count=entry.get('image_count'), declared_page_count=entry.get('page_count'),
            direct_image_count=len(direct), related_image_count=len(related),
            direct_source_sha256=digest,
        )


def read_prepared(record: dict, *, reading: Literal['local', 'frozen'] | None = None,
                  confirmed_empty: bool = False) -> PreparedMaterial:
    if not isinstance(record, dict):
        raise ValueError('attachment_content_incomplete')
    result = PreparedMaterial(record)
    if reading is None:
        return result
    if reading not in {'local', 'frozen'}:
        raise ValueError('attachment_reading_invalid')
    if any(not isinstance(record.get(key, []), list)
           for key in ('images', 'manifest', 'structured_contents')):
        raise ValueError('attachment_content_incomplete')
    images = record.get('images', [])
    if (not confirmed_empty and not images and not record.get('structured_contents')
            and not skipped_video_sources(record.get('manifest', []))):
        raise ValueError('attachment_content_empty')
    for entry in record.get('manifest', []):
        if entry.get('status') not in SUCCESSFUL_PREPARATION_STATUSES | SKIPPED_PREPARATION_STATUSES:
            raise ValueError('attachment_content_incomplete')
        if 'image_count' in entry:
            expected = entry['image_count']
            coverage = result.coverage(entry.get('resource_id'))
            actual = coverage.related_image_count if coverage is not None else 0
            if type(expected) is not int or expected != actual:
                raise ValueError('attachment_page_coverage_incomplete')
            if entry['status'] == 'pdf_rendered' and (expected < 1 or expected != entry.get('page_count')):
                raise ValueError('attachment_page_coverage_incomplete')
    if reading == 'frozen' and any(not _valid_digest(page.get('content_sha256')) for page in images):
        raise ValueError('attachment_page_digest_missing')
    return result
