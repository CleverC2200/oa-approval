from __future__ import annotations

import json
from pathlib import Path

from apps.oa.runtime.control import read_control_bytes


def summarize(report: dict, report_file: Path) -> dict:
    plan = report.get('read_plan')
    summary = {'status': report['status'], 'report_file': str(report_file),
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


def progress(report_file: Path) -> dict:
    report_file = Path(report_file).expanduser().absolute()
    raw = read_control_bytes(report_file, max_bytes=512 * 1024 * 1024)
    if raw is None:
        raise ValueError('attachment_recognition_report_missing')
    try:
        report = json.loads(raw)
        _validate(report)
    except (UnicodeError, ValueError, TypeError, KeyError):
        raise ValueError('attachment_recognition_report_invalid') from None
    return summarize(report, report_file)


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
