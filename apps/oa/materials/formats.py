from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from tempfile import mkdtemp
import time
from typing import Any, Dict, List, Tuple
import xml.etree.ElementTree as ET
import zipfile
import zlib

from apps.oa.runtime import files as material_store

import fitz
from PIL import Image

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
PREPARABLE_IMAGE_EXTS = IMAGE_EXTS | {".heic", ".tif", ".tiff"}
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx"}
PDF_EXTS = {".pdf"}
SPREADSHEET_EXTS = {".xlsx", ".xlsm"}
LEGACY_SPREADSHEET_EXTS = {".xls"}
DOCX_EXTS = {".docx"}
LEGACY_DOC_TIMEOUT_SECONDS = 60
EXTERNAL_PAYLOAD_MAX_BYTES = 200_000_000
XML_EXTS = {".xml"}
OFD_EXTS = {".ofd"}
RAR_EXTS = {".rar"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts", ".ogv", ".vob"}
MAX_DOCX_XML_BYTES = 10_000_000
MAX_DOCX_MEDIA_FILES = 30
MAX_DOCX_MEDIA_BYTES = 50_000_000
MAX_OFD_XML_FILES = 30
MAX_OFD_XML_BYTES = 5_000_000


from apps.oa.materials.prepared import (
    PreparedMaterial, SUCCESSFUL_PREPARATION_STATUSES, SKIPPED_PREPARATION_STATUSES,
    skipped_video_sources,
)


def safe_name(value: str, fallback: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
    return text or fallback


def _bounded_converter_command(command: list[str], max_bytes: int) -> list[str]:
    return [sys.executable, '-c',
            'import os, resource, sys; limit=int(sys.argv[1]); '
            'resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit)); '
            'os.execv(sys.argv[2], sys.argv[2:])',
            str(max_bytes), *command]


def render_pdf(
    path: Path,
    *,
    out_dir: Path,
    resource_id: str,
    compact: bool = False,
) -> tuple[List[Tuple[Path, str, str]], int, List[str]]:
    out: List[Tuple[Path, str, str]] = []
    page_texts: List[str] = []
    doc = fitz.open(path)
    try:
        page_count = len(doc)
        for index in range(page_count):
            page = doc[index]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = pixmap.tobytes("png")
            suffix = ".png"
            if compact and len(image) > 256 * 1024:
                jpeg = pixmap.tobytes("jpeg", jpg_quality=95)
                if len(jpeg) < len(image):
                    image, suffix = jpeg, ".jpg"
            dest = out_dir / f"{safe_name(resource_id, path.stem)}_p{index + 1}{suffix}"
            material_store.write_bytes(dest, image, source={'resource_id': resource_id, 'kind': 'pdf_page'})
            out.append(
                (
                    dest,
                    f"pdf_page_{index + 1}",
                    _file_sha256(dest),
                )
            )
            lines = [
                re.sub(r"\s+", " ", line).strip()
                for line in page.get_text("text").splitlines()
            ]
            page_text = " ".join(line for line in lines if line)
            if page_text:
                page_texts.append(f"P{index + 1}: {page_text}")
    finally:
        doc.close()
    return out, page_count, page_texts


def _reap_office_converter(process: subprocess.Popen, prefix: str) -> None:
    try:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(f"{prefix}_conversion_failed") from None


def render_office_document(
    path: Path, *, out_dir: Path, resource_id: str,
) -> tuple[List[Tuple[Path, str, str]], int, List[str]]:
    presentation = path.suffix.lower() == ".pptx"
    prefix = "pptx" if presentation else "legacy_doc"
    expected_pages = None
    if presentation:
        try:
            with zipfile.ZipFile(path) as archive:
                member = archive.getinfo("ppt/presentation.xml")
                if member.file_size > MAX_DOCX_XML_BYTES:
                    raise ValueError("presentation_xml_too_large")
                root = ET.fromstring(archive.read(member))
            namespace = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
            expected_pages = len(root.findall(f"{namespace}sldIdLst/{namespace}sldId"))
            if expected_pages < 1:
                raise ValueError("presentation_slides_missing")
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, zlib.error, ET.ParseError):
            raise RuntimeError("pptx_content_invalid") from None
    pdf_filter = (
        'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"},'
        '"UseLosslessCompression":{"type":"boolean","value":"true"},'
        '"ReduceImageResolution":{"type":"boolean","value":"false"}}'
        if presentation else "pdf:writer_pdf_Export"
    )
    converter = shutil.which(os.environ.get("OA_LIBREOFFICE_BIN") or "soffice")
    if converter is None:
        raise RuntimeError(f"{prefix}_converter_unavailable")
    temporary = Path(mkdtemp(prefix="pptx-" if presentation else "legacy-doc-", dir=out_dir))
    output_limit = EXTERNAL_PAYLOAD_MAX_BYTES
    try:
        work = temporary.resolve()
        source = work / ("source.pptx" if presentation else "source.doc")
        shutil.copyfile(path, source)
        profile = work / "profile"
        profile.mkdir()
        (profile / "registrymodifications.xcu").write_text(
            '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>', encoding="utf-8",
        )
        environment = os.environ.copy()
        home = work / 'home'
        home.mkdir()
        environment.update(TMPDIR=str(work), TMP=str(work), TEMP=str(work), HOME=str(home),
                           XDG_CACHE_HOME=str(home / '.cache'), XDG_CONFIG_HOME=str(home / '.config'))
        mac_fonts = Path("/System/Library/Fonts/Supplemental")
        if (sys.platform == "darwin" and mac_fonts.is_dir()
                and not environment.get("FONTCONFIG_FILE")):
            fontconfig = ET.Element("fontconfig")
            ET.SubElement(fontconfig, "dir").text = str(mac_fonts)
            ET.SubElement(fontconfig, "cachedir").text = str(work / "font-cache")
            fontconfig_path = work / "fonts.conf"
            ET.ElementTree(fontconfig).write(fontconfig_path, encoding="utf-8")
            environment["FONTCONFIG_FILE"] = str(fontconfig_path)
        try:
            process = subprocess.Popen(
                _bounded_converter_command([converter, f"-env:UserInstallation={profile.as_uri()}",
                    "--headless", "--nologo", "--nodefault", "--norestore",
                    "--convert-to", pdf_filter, "--outdir", str(work), str(source)], output_limit),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, env=environment,
            )
        except OSError:
            raise RuntimeError(f"{prefix}_converter_unavailable") from None
        try:
            returncode = process.wait(timeout=LEGACY_DOC_TIMEOUT_SECONDS)
        except BaseException as error:
            try:
                _reap_office_converter(process, prefix)
            except BaseException as cleanup_error:
                raise error from cleanup_error
            raise
        else:
            _reap_office_converter(process, prefix)
        pdf = work / "source.pdf"
        if returncode != 0 or not pdf.is_file() or pdf.stat().st_size > output_limit:
            raise RuntimeError(f"{prefix}_conversion_failed")
        converted = pdf.read_bytes()
        pdf = temporary / 'source.pdf'
        material_store.write_bytes(pdf, converted,
                                   source={'resource_id': resource_id, 'kind': 'converted_pdf'})
        try:
            rendered = render_pdf(pdf, out_dir=out_dir, resource_id=resource_id)
        except Exception:
            raise RuntimeError(f"{prefix}_conversion_failed") from None
        if (rendered[1] < 1 or len(rendered[0]) != rendered[1]
                or (expected_pages is not None and rendered[1] != expected_pages)):
            raise RuntimeError(f"{prefix}_conversion_failed")
        return rendered
    finally:
        shutil.rmtree(temporary)


render_legacy_doc = render_office_document

def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_heic_content(path: Path) -> bool:
    with path.open("rb") as source:
        header = source.read(64)
    if len(header) < 12 or header[4:8] != b"ftyp":
        return False
    brands = {header[8:12]}
    brands.update(
        header[index : index + 4]
        for index in range(16, len(header) - 3, 4)
    )
    return bool(brands & HEIC_BRANDS)


def _convert_heic_to_png(path: Path, *, out_dir: Path, resource_id: str) -> Path:
    converter = Path("/usr/bin/sips")
    if not converter.exists():
        raise RuntimeError("heic_converter_unavailable")
    destination = out_dir / f"{safe_name(resource_id, path.stem)}_heic.png"
    output_limit = EXTERNAL_PAYLOAD_MAX_BYTES
    with material_store.bounded_output(destination, max_bytes=output_limit):
        result = subprocess.run(
            _bounded_converter_command([str(converter), "-s", "format", "png", str(path), "--out", str(destination)],
                                       output_limit),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or not destination.is_file() or destination.stat().st_size < 1:
            raise RuntimeError("heic_conversion_failed")
    return destination


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _amount_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        amount = round(float(value), 2)
    else:
        text = _cell_text(value).replace(",", "")
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            return None
        amount = round(float(text), 2)
    return amount


def spreadsheet_fact(path: Path) -> Dict[str, Any]:
    import openpyxl

    with path.open("rb") as source:
        workbook = openpyxl.load_workbook(source, data_only=True, read_only=True)
        try:
            sheet_summaries: List[str] = []
            key_text: List[str] = []
            for sheet in workbook.worksheets:
                sheet.reset_dimensions()
                last_row = 0
                last_column = 0
                for row_index, row in enumerate(
                    sheet.iter_rows(values_only=True), start=1
                ):
                    values = [_cell_text(value) for value in row]
                    nonempty = [
                        (column, value)
                        for column, value in enumerate(values, 1)
                        if value
                    ]
                    if not nonempty:
                        continue
                    last_row = row_index
                    last_column = max(last_column, nonempty[-1][0])
                    located_values = [
                        f"C{column}={value}" for column, value in nonempty
                    ]
                    key_text.append(
                        f"{sheet.title}!R{row_index}: {' | '.join(located_values)}"
                    )
                sheet_summaries.append(
                    f"{sheet.title}({last_row}行{last_column}列)"
                )
        finally:
            workbook.close()
    summary = f"Excel工作簿，包含工作表：{', '.join(sheet_summaries)}。"
    return {
        "document_type": "spreadsheet",
        "summary": summary,
        "key_text": key_text,
    }


def _is_ooxml_spreadsheet(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            archive.getinfo("[Content_Types].xml")
            archive.getinfo("xl/workbook.xml")
    except (KeyError, OSError, zipfile.BadZipFile):
        return False
    return True


def legacy_spreadsheet_fact(path: Path) -> Dict[str, Any]:
    try:
        import xlrd
    except Exception as err:
        raise RuntimeError(
            "旧 Excel .xls 读取需要 xlrd，请另存为 .xlsx 或导出 PDF/图片后上传"
        ) from err

    workbook = xlrd.open_workbook(str(path), on_demand=True)
    try:
        sheet_summaries: List[str] = []
        key_text: List[str] = []
        for sheet in workbook.sheets():
            sheet_summaries.append(f"{sheet.name}({sheet.nrows}行{sheet.ncols}列)")
            for row_index in range(sheet.nrows):
                values = [_cell_text(value) for value in sheet.row_values(row_index)]
                nonempty = [value for value in values if value]
                if not nonempty:
                    continue
                key_text.append(
                    f"{sheet.name}!R{row_index + 1}: {' | '.join(nonempty)}"
                )
    finally:
        release = getattr(workbook, "release_resources", None)
        if callable(release):
            release()
    summary = f"旧Excel工作簿，包含工作表：{', '.join(sheet_summaries)}。"
    return {
        "document_type": "legacy_spreadsheet",
        "summary": summary,
        "key_text": key_text,
    }


def _xml_local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def _docx_element_text(element: ET.Element) -> str:
    return "".join(
        str(item.text or "")
        for item in element.iter()
        if _xml_local_name(item.tag) == "t"
    ).strip()


def _docx_text_items(root: ET.Element) -> List[str]:
    body = next(
        (item for item in root.iter() if _xml_local_name(item.tag) == "body"), None
    )
    if body is None:
        return []
    texts: List[str] = []
    for child in body:
        kind = _xml_local_name(child.tag)
        if kind == "p":
            text = _docx_element_text(child)
            if text:
                texts.append(text)
        elif kind == "tbl":
            for row in child.iter():
                if _xml_local_name(row.tag) != "tr":
                    continue
                cells = [
                    _docx_element_text(cell)
                    for cell in row
                    if _xml_local_name(cell.tag) == "tc" and _docx_element_text(cell)
                ]
                if cells:
                    texts.append(" | ".join(cells))
    return texts


def docx_fact_and_images(
    path: Path,
    *,
    resource_id: str,
    out_dir: Path,
) -> tuple[dict, list[_ImageDraft]]:
    if not zipfile.is_zipfile(path):
        raise ValueError("DOCX不是有效ZIP容器")
    images: list[_ImageDraft] = []
    with zipfile.ZipFile(path) as archive:
        try:
            document_info = archive.getinfo("word/document.xml")
        except KeyError as err:
            raise ValueError("DOCX缺少word/document.xml") from err
        if document_info.file_size > MAX_DOCX_XML_BYTES:
            raise ValueError("DOCX正文超过读取上限")
        root = ET.fromstring(archive.read(document_info))
        texts = _docx_text_items(root)
        media = [
            item
            for item in archive.infolist()
            if not item.is_dir()
            and item.filename.startswith("word/media/")
            and Path(item.filename).suffix.lower() in IMAGE_EXTS
        ]
        if len(media) > MAX_DOCX_MEDIA_FILES:
            raise ValueError("DOCX内嵌图片数量超过读取上限")
        total_media_bytes = sum(int(item.file_size or 0) for item in media)
        if total_media_bytes > MAX_DOCX_MEDIA_BYTES:
            raise ValueError("DOCX内嵌图片总大小超过读取上限")
        for index, item in enumerate(media, 1):
            suffix = Path(item.filename).suffix.lower()
            target = (
                out_dir / f"{safe_name(resource_id, path.stem)}_docx_{index}{suffix}"
            )
            material_store.write_bytes(target, archive.read(item), source={'resource_id': resource_id, 'kind': 'docx_media'})
            images.append(_ImageDraft(
                {'path': target, 'kind': 'docx_image', 'content_sha256': _file_sha256(target)},
                resource_suffix=f'::docx_media::{index}', name_suffix=f'::{item.filename}',
                parent_is_resource=True,
            ))
    if not texts and not images:
        raise ValueError("DOCX未抽取到正文、表格或内嵌图片")
    summary = f"Word文档，读取到{len(texts)}条正文/表格文本"
    if images:
        summary += f"和{len(images)}张内嵌图片"
    return (
        {
            "document_type": "word_document",
            "summary": summary + "。",
            "key_text": texts,
        },
        images,
    )


def _clean_invoice_amount(value: Any) -> float | None:
    text = _cell_text(value).replace(",", "")
    text = re.sub(r"^[¥￥]\s*", "", text)
    return _amount_value(text)


def _append_amount(amounts: List[float], value: Any) -> None:
    amount = _clean_invoice_amount(value)
    if amount is not None and amount not in amounts:
        amounts.append(amount)


def _xml_values(root: ET.Element) -> Dict[str, List[str]]:
    values: Dict[str, List[str]] = {}
    for element in root.iter():
        text = _cell_text(element.text)
        if not text:
            continue
        values.setdefault(_xml_local_name(element.tag), []).append(text)
    return values


def _first_xml_value(values: Dict[str, List[str]], *names: str) -> str:
    for name in names:
        for item in values.get(name, []):
            if item:
                return item
    return ""


def _invoice_key_text(label_values: List[Tuple[str, str]]) -> List[str]:
    return [f"{label}: {value}" for label, value in label_values if value]


def xml_invoice_fact(path: Path) -> Dict[str, Any]:
    root = ET.parse(path).getroot()
    values = _xml_values(root)
    invoice_id = _first_xml_value(values, "EIid", "InvoiceNumber", "InvoiceNo")
    buyer = _first_xml_value(values, "BuyerName", "PurchaserName")
    seller = _first_xml_value(values, "SellerName")
    issued_at = _first_xml_value(values, "RequestTime", "IssueTime", "InvoiceDate")
    item_name = _first_xml_value(values, "ItemName", "GoodsName", "ProjectName")
    total_with_tax = _first_xml_value(
        values,
        "TotalTax-includedAmount",
        "TotaltaxIncludedAmount",
        "TotalTaxIncludedAmount",
    )
    amount_without_tax = _first_xml_value(values, "TotalAmWithoutTax", "Amount")
    tax_amount = _first_xml_value(values, "TotalTaxAm", "ComTaxAm")
    amounts: List[float] = []
    for tag_name in (
        "TotalTax-includedAmount",
        "TotaltaxIncludedAmount",
        "TotalTaxIncludedAmount",
        "TotalAmWithoutTax",
        "TotalTaxAm",
        "Amount",
        "ComTaxAm",
    ):
        for value in values.get(tag_name, []):
            _append_amount(amounts, value)
    key_text = _invoice_key_text(
        [
            ("发票号", invoice_id),
            ("购买方", buyer),
            ("销售方", seller),
            ("开票时间", issued_at),
            ("项目名称", item_name),
            ("价税合计", total_with_tax),
            ("不含税金额", amount_without_tax),
            ("税额", tax_amount),
        ]
    )
    summary_parts = ["XML电子发票"]
    if invoice_id:
        summary_parts.append(f"发票号{invoice_id}")
    if seller:
        summary_parts.append(f"销售方{seller}")
    if buyer:
        summary_parts.append(f"购买方{buyer}")
    if total_with_tax:
        summary_parts.append(f"价税合计{total_with_tax}")
    return {
        "document_type": "电子发票XML",
        "summary": "，".join(summary_parts) + "。",
        "amounts": amounts,
        "key_text": key_text,
    }


def _useful_ofd_text(text: str) -> bool:
    value = _cell_text(text)
    if not value or value in {"¥", "￥"}:
        return False
    if len(value) > 300:
        return False
    if re.fullmatch(r"\d+(?:\s+\d+)+", value):
        return False
    if re.fullmatch(r"[MLCQA-Z0-9.\s-]+", value) and any(
        mark in value for mark in (" M", " L", " C", "M ", "L ")
    ):
        return False
    return True


def _ofd_texts(path: Path) -> List[str]:
    if not zipfile.is_zipfile(path):
        raise ValueError("OFD不是有效ZIP容器")
    texts: List[str] = []
    total_bytes = 0
    with zipfile.ZipFile(path) as archive:
        content_members = [
            item
            for item in archive.infolist()
            if not item.is_dir() and item.filename.lower().endswith("content.xml")
        ]
        xml_members = content_members or [
            item
            for item in archive.infolist()
            if not item.is_dir() and item.filename.lower().endswith(".xml")
        ]
        if len(xml_members) > MAX_OFD_XML_FILES:
            raise ValueError("OFD内部XML文件数量超过读取上限")
        for member in xml_members:
            total_bytes += int(member.file_size or 0)
            if total_bytes > MAX_OFD_XML_BYTES:
                raise ValueError("OFD内部XML超过读取上限")
            try:
                root = ET.fromstring(archive.read(member))
            except Exception:
                continue
            for element in root.iter():
                if content_members and _xml_local_name(element.tag) != "TextCode":
                    continue
                text = _cell_text(element.text)
                if _useful_ofd_text(text):
                    texts.append(text)
    return texts


def _first_matching_text(texts: List[str], pattern: str) -> str:
    for text in texts:
        if re.search(pattern, text):
            return text
    return ""


def _ofd_amounts(texts: List[str]) -> List[float]:
    amounts: List[float] = []
    for index, text in enumerate(texts):
        amount = _clean_invoice_amount(text)
        if amount is None:
            continue
        previous_text = texts[index - 1] if index > 0 else ""
        if previous_text in {"次", "件", "个", "张"}:
            continue
        _append_amount(amounts, text)
    return amounts


def _ofd_key_text_tail(texts: List[str], existing: List[str]) -> List[str]:
    generic_labels = {
        "发票号码：",
        "开票日期：",
        "名称：",
        "统一社会信用代码/纳税人识别号：",
        "项目名称",
        "单  位",
        "数  量",
        "单  价",
        "金  额",
        "税率/征收率",
        "税  额",
        "合",
        "计",
        "价税合计（大写）",
        "（小写）",
        "备",
        "注",
        "开票人：",
    }
    seen = set(existing)
    out: List[str] = []
    for text in texts:
        if text in seen or text in generic_labels:
            continue
        if len(text) <= 1 and not text.isdigit():
            continue
        seen.add(text)
        out.append(text)
    return out


def ofd_invoice_fact(path: Path) -> Dict[str, Any]:
    texts = _ofd_texts(path)
    if not texts:
        raise ValueError("OFD未抽取到文本内容")
    invoice_id = _first_matching_text(texts, r"^\d{20}$")
    companies = [
        text for text in texts if "公司" in text and not re.search(r"名称|识别号", text)
    ]
    buyer = companies[0] if companies else ""
    seller = companies[1] if len(companies) > 1 else ""
    issued_at = _first_matching_text(texts, r"^\d{4}年\d{2}月\d{2}日$")
    item_name = _first_matching_text(texts, r"运输服务|货物运输")
    amounts = _ofd_amounts(texts)
    key_text = _invoice_key_text(
        [
            ("发票号", invoice_id),
            ("购买方", buyer),
            ("销售方", seller),
            ("开票日期", issued_at),
            ("项目名称", item_name),
        ]
    )
    key_text.extend(_ofd_key_text_tail(texts, key_text))
    summary_parts = ["OFD电子发票"]
    if invoice_id:
        summary_parts.append(f"发票号{invoice_id}")
    if seller:
        summary_parts.append(f"销售方{seller}")
    if buyer:
        summary_parts.append(f"购买方{buyer}")
    if amounts:
        summary_parts.append(
            "金额候选" + "、".join(f"{amount:.2f}" for amount in amounts[:6])
        )
    return {
        "document_type": "电子发票OFD",
        "summary": "，".join(summary_parts) + "。",
        "amounts": amounts,
        "key_text": key_text,
    }


@dataclass(frozen=True, slots=True)
class _ImageDraft:
    record: dict
    resource_suffix: str = ''
    name_suffix: str = ''
    parent_is_resource: bool = False


@dataclass(frozen=True, slots=True)
class _ConversionSource:
    path: Path
    resource_id: str
    parent_resource_id: str
    source_content_sha256: str
    suffix: str

    def image(self, path: Path, kind: str, content_sha256: str, **metadata) -> _ImageDraft:
        return _ImageDraft({'path': path, 'kind': kind, 'source_content_sha256': self.source_content_sha256,
                            'content_sha256': content_sha256, **metadata})

    def result(self, status: str, *, images=(), structured=(), manifest_parent=False, **metadata) -> _ConversionResult:
        return _ConversionResult(tuple(images), ({'status': status, **metadata},),
                                 tuple(structured), manifest_parent)


@dataclass(frozen=True, slots=True)
class _ResourceResult:
    images: tuple[dict, ...]
    manifest: tuple[dict, ...]
    structured_contents: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class _ConversionResult:
    images: tuple[_ImageDraft, ...]
    manifest: tuple[dict, ...]
    structured_contents: tuple[dict, ...]
    manifest_parent: bool = False

    def bind(self, item: dict, *, source_content_sha256: str) -> _ResourceResult:
        resource_id = str(item.get('resource_id') or '')
        name = str(item.get('name') or Path(str(item.get('path') or '')).name)
        parent = str(item.get('parent_resource_id') or '')

        def bind_record(record, *, resource_suffix='', name_suffix='', parent_id=''):
            return {'resource_id': resource_id + resource_suffix, 'name': name + name_suffix,
                    **({'parent_resource_id': parent_id} if parent_id else {}), **deepcopy(record),
                    'source_resource_id': resource_id, 'source_path': str(item.get('path') or ''),
                    'source_content_sha256': source_content_sha256,
                    **({'archive_path': item['archive_path']} if 'archive_path' in item else {})}

        return _ResourceResult(
            tuple(bind_record(image.record, resource_suffix=image.resource_suffix, name_suffix=image.name_suffix,
                              parent_id=parent or (resource_id if image.parent_is_resource else ''))
                  for image in self.images),
            tuple(bind_record(row, parent_id=parent if self.manifest_parent else '') for row in self.manifest),
            tuple(bind_record(row, parent_id=parent) for row in self.structured_contents))


def _conversion_source(item: dict) -> _ConversionSource:
    path = Path(str(item.get('path') or '')).expanduser()
    suffix = path.suffix.lower()
    if path.exists() and not suffix:
        try:
            with Image.open(path) as image:
                suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp',
                          'BMP': '.bmp', 'TIFF': '.tiff'}.get(image.format, '')
                image.verify()
            if suffix:
                with Image.open(path) as image:
                    image.load()
        except (OSError, ValueError, SyntaxError):
            suffix = ''
    return _ConversionSource(path, str(item.get('resource_id') or ''),
        str(item.get('parent_resource_id') or ''),
        _file_sha256(path) if path.exists() else '', suffix)


def _convert_image(source: _ConversionSource, *, out_dir: Path) -> _ConversionResult:
    image_path, status = source.path, 'image'
    digest = source.source_content_sha256
    if _is_heic_content(source.path):
        image_path = _convert_heic_to_png(source.path, out_dir=out_dir,
                                        resource_id=source.resource_id or source.path.stem)
        status, digest = 'image_converted', _file_sha256(image_path)
    elif source.parent_resource_id or not source.path.suffix:
        image_path = out_dir / f'{safe_name(source.resource_id, source.path.stem)}_archive{source.suffix}'
        with source.path.open('rb') as original:
            material_store.copy_stream(image_path, original, expected_bytes=source.path.stat().st_size,
                source={'resource_id': source.resource_id, 'kind': 'image_copy'})
    return source.result(status, images=[source.image(image_path, 'image', digest)], image_count=1)


def _convert_pages(source: _ConversionSource, *, out_dir: Path, compact: bool) -> _ConversionResult:
    renderer = render_office_document if source.suffix in {'.doc', '.pptx'} else render_pdf
    try:
        rendered, page_count, page_texts = renderer(
            source.path, out_dir=out_dir, resource_id=source.resource_id or source.path.stem,
            **({'compact': True} if compact and source.suffix in PDF_EXTS else {}))
    except subprocess.TimeoutExpired:
        if source.suffix == '.pptx':
            raise RuntimeError('attachment_content_incomplete') from None
        raise RuntimeError('legacy_doc_conversion_timeout') from None
    except RuntimeError:
        if source.suffix == '.pptx':
            raise RuntimeError('attachment_content_incomplete') from None
        raise
    structured = [{'document_type': 'pdf_text',
                   'summary': f'PDF文档，读取到{len(page_texts)}页文本。',
                   'key_text': page_texts, 'source_content_sha256': source.source_content_sha256}] if page_texts else []
    return source.result('pdf_rendered',
        images=[source.image(path, kind, digest) for path, kind, digest in rendered],
        structured=structured, image_count=len(rendered), page_count=page_count)


def _convert_spreadsheet(source: _ConversionSource, *, out_dir: Path, compact: bool) -> _ConversionResult:
    from apps.oa.materials import spreadsheet_images

    images = []
    try:
        extracted = spreadsheet_images.extract(source.path, out_dir,
                    safe_name(source.resource_id, source.path.stem), compact=compact)
        fact = spreadsheet_fact(source.path)
        for number, (path, cell) in enumerate(extracted, 1):
            images.append(source.image(path, f'spreadsheet_image_{number}', _file_sha256(path),
                                       spreadsheet_cell=cell))
            fact['key_text'].append(f'{cell}: embedded image {number}')
    except Exception as error:
        return source.result('spreadsheet_failed', images=images, path=str(source.path),
                             error_message=str(error)[:500])
    return source.result('spreadsheet_read', images=images, structured=[fact],
                         image_count=len(images), path=str(source.path))


def _convert_structured(source: _ConversionSource, *, reader, success: str, failure: str) -> _ConversionResult:
    try:
        fact = reader(source.path)
    except Exception as error:
        return source.result(failure, path=str(source.path), error_message=str(error)[:500])
    return source.result(success, structured=[fact], path=str(source.path))


def _convert_docx(source: _ConversionSource, *, out_dir: Path) -> _ConversionResult:
    try:
        fact, images = docx_fact_and_images(source.path, resource_id=source.resource_id, out_dir=out_dir)
    except Exception as error:
        return source.result('docx_failed', path=str(source.path), error_message=str(error)[:500])
    return source.result('docx_read', images=images, structured=[fact],
                         path=str(source.path), image_count=len(images))


_STRUCTURED_READERS = {
    '.xls': (legacy_spreadsheet_fact, 'legacy_spreadsheet_read', 'legacy_spreadsheet_unsupported'),
    '.xml': (xml_invoice_fact, 'xml_invoice_read', 'xml_failed'),
    '.ofd': (ofd_invoice_fact, 'ofd_invoice_read', 'ofd_failed'),
}


def _convert_attachment(source: _ConversionSource, *, out_dir: Path,
                        compact_pdf: bool, compact_spreadsheet: bool) -> _ConversionResult:
    if not source.path.exists():
        return source.result('missing_file', path=str(source.path))
    suffix = source.suffix
    if suffix in PREPARABLE_IMAGE_EXTS:
        return _convert_image(source, out_dir=out_dir)
    if suffix in VIDEO_EXTS:
        return source.result('video_skipped',
            manifest_parent=True,
            source_content_sha256=source.source_content_sha256,
            body_read_status='not_read', image_count=0, reason='video_content_excluded')
    if suffix in PDF_EXTS | {'.doc', '.pptx'}:
        return _convert_pages(source, out_dir=out_dir, compact=compact_pdf)
    if suffix in SPREADSHEET_EXTS or (suffix in LEGACY_SPREADSHEET_EXTS and _is_ooxml_spreadsheet(source.path)):
        return _convert_spreadsheet(source, out_dir=out_dir, compact=compact_spreadsheet)
    if suffix in DOCX_EXTS:
        return _convert_docx(source, out_dir=out_dir)
    if suffix in RAR_EXTS:
        return source.result('rar_archive_unsupported', path=str(source.path),
            error_message='RAR 压缩包未完成安全展开，请确认 RAR 工具可用或展开为 PDF/图片后上传')
    if suffix in _STRUCTURED_READERS:
        reader, success, failure = _STRUCTURED_READERS[suffix]
        return _convert_structured(source, reader=reader, success=success, failure=failure)
    return source.result('unsupported_file', path=str(source.path))


class _Preparation:
    def __init__(self, *, out_dir: Path, compact_pdf: bool, compact_spreadsheet: bool):
        self.out_dir = out_dir
        self.compact_pdf = compact_pdf
        self.compact_spreadsheet = compact_spreadsheet
        self._spreadsheet_contents: dict[tuple[str, str], _ConversionResult] = {}

    def convert(self, item: dict) -> _ResourceResult:
        source = _conversion_source(item)
        key = ((source.path.suffix.lower(), source.source_content_sha256)
               if source.path.suffix.lower() in SPREADSHEET_EXTS | LEGACY_SPREADSHEET_EXTS
               and source.path.is_file() else None)
        result = self._spreadsheet_contents.get(key) if key is not None else None
        if result is None:
            result = _convert_attachment(source, out_dir=self.out_dir, compact_pdf=self.compact_pdf,
                                         compact_spreadsheet=self.compact_spreadsheet)
            if key is not None and all(entry.get('status') in SUCCESSFUL_PREPARATION_STATUSES | SKIPPED_PREPARATION_STATUSES
                                       for entry in result.manifest):
                self._spreadsheet_contents[key] = result
        return result.bind(item, source_content_sha256=source.source_content_sha256)

    def run(self, attachments: list[dict]) -> PreparedMaterial:
        images, manifest, structured, validation = [], [], [], []
        for item in attachments:
            extracted = item.get('extracted_files') if isinstance(item.get('extracted_files'), list) else []
            results = [self.convert(child) for child in extracted if isinstance(child, dict)] if extracted else [self.convert(item)]
            for result in results:
                images.extend(result.images)
                structured.extend(result.structured_contents)
                validation.extend(result.manifest)
            if extracted:
                children = [entry for result in results for entry in result.manifest]
                manifest.append({'resource_id': str(item.get('resource_id') or ''),
                    'name': str(item.get('name') or ''), 'status': 'archive_extracted',
                    'archive_extracted_count': len(extracted),
                    'image_count': sum(len(result.images) for result in results),
                    'skipped_videos': skipped_video_sources(children)})
            else:
                manifest.extend(results[0].manifest)
        if any(str(entry.get('status') or '') not in SUCCESSFUL_PREPARATION_STATUSES | SKIPPED_PREPARATION_STATUSES
               or (entry.get('status') == 'pdf_rendered' and (
                   int(entry.get('image_count') or 0) < 1
                   or int(entry.get('image_count') or 0) != int(entry.get('page_count') or 0)))
               for entry in validation):
            raise RuntimeError('attachment_content_incomplete')
        return PreparedMaterial({'images': images, 'manifest': manifest, 'structured_contents': structured})


def prepare_materials(attachments: List[Dict[str, Any]], *, out_dir: Path,
                      compact_pdf: bool = False, compact_spreadsheet: bool = False) -> PreparedMaterial:
    out_dir.mkdir(parents=True, exist_ok=True)
    return _Preparation(out_dir=out_dir, compact_pdf=compact_pdf,
                        compact_spreadsheet=compact_spreadsheet).run(attachments)
