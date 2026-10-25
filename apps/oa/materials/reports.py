from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any, NotRequired, TypedDict


class RecognitionRecord(TypedDict):
    kind: str
    parts: list[dict[str, Any]]
    schema_version: NotRequired[int]
    status: NotRequired[str]
    model: NotRequired[str]
    transport: NotRequired[str]
    prompt: NotRequired[str]
    created_at: NotRequired[str]
    completed_at: NotRequired[str | None]
    report_file: NotRequired[str]
    sources: NotRequired[list[dict[str, Any]]]
    read_plan: NotRequired[list[str]]
    coverage: NotRequired[str]
    selection: NotRequired[dict[str, Any] | None]
    material_binding: NotRequired[dict[str, Any]]
    attachment_presence: NotRequired[dict[str, Any]]
    skipped_sources: NotRequired[list[dict[str, Any]]]
    reason: NotRequired[str]
    region: NotRequired[list[int]]
    error_code: NotRequired[str]
    failed_part: NotRequired[dict[str, Any]]
    failed_response: NotRequired[dict[str, Any]]
    observations_file: NotRequired[str]


class ProgressSummary(TypedDict):
    status: str
    report_file: str
    completed_parts: int
    planned_parts: int | None
    coverage: str | None
    selection: dict[str, Any] | None
    region: NotRequired[list[int]]
    reason: NotRequired[str]
    error_code: NotRequired[str]
    failed_part: NotRequired[dict[str, Any]]


def parse_report(raw: bytes) -> RecognitionRecord:
    try:
        report = json.loads(raw)
        _validate(report)
    except (UnicodeError, ValueError, TypeError, KeyError):
        raise ValueError('attachment_recognition_report_invalid') from None
    return report


def summarize(report: Mapping[str, Any], report_file: Path) -> ProgressSummary:
    plan = report.get('read_plan')
    summary: ProgressSummary = {'status': report['status'], 'report_file': str(report_file),
               'completed_parts': len(report['parts']),
               'planned_parts': len(plan) if plan is not None else None,
               'coverage': report.get('coverage'), 'selection': None}
    if report.get('selection') is not None:
        summary['selection'] = {name: report['selection'][name] for name in ('resource_id', 'page')
                                if name in report['selection']}
    for name in ('region', 'reason', 'error_code'):
        if name in report:
            summary[name] = report[name]
    if 'failed_part' in report:
        names = ('part_id', 'kind', 'resource_id', 'prepared_resource_id', 'page_number', 'evidence_ref',
                 'failed_tile_box', 'region', 'rotate')
        summary['failed_part'] = {name: report['failed_part'][name] for name in names if name in report['failed_part']}
    return summary


def review_contents(recognition: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(recognition, dict) or recognition.get('kind') != 'deepseek_attachment_recognition':
        return ()
    return tuple({**part, 'locator': part['evidence_ref'], 'source': part.get('source') or 'deepseek',
                  'content': part['answer'], 'status': 'read'} for part in recognition['parts'])


def supplemental_observations(
        report: Mapping[str, Any], *, report_file: Path, prompt: str,
) -> tuple[dict[str, Any], ...]:
    if report['status'] != 'complete':
        return ()
    observations = []
    for part in report['parts']:
        locator = part['evidence_ref'] + '#supplemental'
        if part['kind'] == 'image':
            locator += f'&rotate={part["rotate"]}'
            if 'region' in part:
                locator += '&region=' + ','.join(str(value) for value in part['region'])
        observations.append({**part, 'locator': locator, 'content': part['answer'], 'status': 'read',
                             'coverage': report['coverage'], 'selection': report['selection'],
                             'report_file': str(report_file), 'prompt': prompt,
                             'material_binding': report['material_binding']})
    return tuple(observations)


def _validate(report: dict) -> None:
    if (not isinstance(report, dict) or report.get('schema_version') != 1
            or report.get('kind') != 'deepseek_attachment_recognition'
            or report.get('status') not in {'running', 'complete', 'skipped', 'failed'}
            or not isinstance(report.get('parts'), list)):
        raise ValueError
    ids = [part['part_id'] for part in report['parts'] if isinstance(part, dict)]
    if (len(ids) != len(report['parts']) or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError
    plan = report.get('read_plan')
    if plan is not None:
        if (not isinstance(plan, list) or any(not isinstance(value, str) or not value for value in plan)
                or len(set(plan)) != len(plan) or ids != plan[:len(ids)]):
            raise ValueError
        if report['status'] == 'complete' and ids != plan:
            raise ValueError
        if report['status'] == 'skipped' and plan:
            raise ValueError
    if report['status'] != 'running' and not isinstance(report.get('completed_at'), str):
        raise ValueError
    if report.get('coverage') not in {None, 'all_prepared_content', 'no_attachments', 'non_video_only',
                                      'selected_resource', 'selected_page', 'image_region'}:
        raise ValueError
    if report.get('selection') is not None:
        selection = report['selection']
        if (not isinstance(selection, dict) or not isinstance(selection.get('resource_id'), str)
                or ('page' in selection and (type(selection['page']) is not int or selection['page'] < 1))):
            raise ValueError
    for name in ('reason', 'error_code'):
        if name in report and not isinstance(report[name], str):
            raise ValueError
    if 'region' in report and not _box(report['region']):
        raise ValueError
    if 'failed_part' in report:
        part = report['failed_part']
        if not isinstance(part, dict):
            raise ValueError
        for name in ('part_id', 'kind', 'resource_id', 'prepared_resource_id', 'evidence_ref'):
            if name in part and not isinstance(part[name], str):
                raise ValueError
        for name in ('page_number', 'rotate'):
            if name in part and type(part[name]) is not int:
                raise ValueError
        for name in ('failed_tile_box', 'region'):
            if name in part and not _box(part[name]):
                raise ValueError


def _box(value) -> bool:
    return isinstance(value, list) and len(value) == 4 and all(type(item) is int for item in value)
