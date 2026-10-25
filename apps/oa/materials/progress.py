from __future__ import annotations

from pathlib import Path

from apps.oa.materials import reports
from apps.oa.runtime.control import read_control_bytes


def progress(report_file: Path) -> reports.ProgressSummary:
    report_file = Path(report_file).expanduser().absolute()
    raw = read_control_bytes(report_file, max_bytes=512 * 1024 * 1024)
    if raw is None:
        raise ValueError('attachment_recognition_report_missing')
    return reports.summarize(reports.parse_report(raw), report_file)
