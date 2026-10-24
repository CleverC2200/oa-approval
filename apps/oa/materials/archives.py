from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import threading
from typing import Any, Dict, List, Tuple
import zipfile

from apps.oa.materials.resources import resource_name, resource_key, safe_filename
from apps.oa.runtime import files as material_store


EXTRACT_DIR_NAME = "extracted"
ZIP_EXTS = {".zip"}
RAR_EXTS = {".rar"}
ARCHIVE_MAX_FILES = int(os.environ.get("OA_ATTACHMENT_ARCHIVE_MAX_FILES", "128"))
ARCHIVE_MAX_FILE_BYTES = int(
    os.environ.get("OA_ATTACHMENT_ARCHIVE_MAX_FILE_BYTES", str(50 * 1024 * 1024))
)
ARCHIVE_MAX_TOTAL_BYTES = int(
    os.environ.get("OA_ATTACHMENT_ARCHIVE_MAX_TOTAL_BYTES", str(200 * 1024 * 1024))
)
ARCHIVE_MAX_DEPTH = 3
ARCHIVE_EXTRACT_TIMEOUT_SEC = int(
    os.environ.get("OA_ATTACHMENT_ARCHIVE_EXTRACT_TIMEOUT_SEC", "30")
)


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def _is_archive_metadata(member_name: str) -> bool:
    parts = [part for part in str(member_name or "").split("/") if part]
    return "__MACOSX" in parts or any(part.startswith("._") for part in parts)


def _safe_archive_target(root: Path, member_name: str) -> Path | None:
    name = str(member_name or "")
    if not name or name.startswith("/") or "\\" in name:
        return None
    target = (root / name).resolve()
    try:
        target.relative_to(root.resolve())
    except Exception:
        return None
    return target


def extract_zip_safely(
    path: Path, *, dest_dir: Path, resource: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], str]:
    key = safe_filename(resource_key(resource), "archive")
    extract_root = dest_dir / EXTRACT_DIR_NAME / key
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    extracted: List[Dict[str, Any]] = []
    total_size = 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > ARCHIVE_MAX_FILES:
                return (
                    [],
                    f"{resource_name(resource) or path.name}: zip 文件数超过限制 {ARCHIVE_MAX_FILES}",
                )
            for index, info in enumerate(infos, 1):
                if _is_zip_symlink(info):
                    return (
                        [],
                        f"{resource_name(resource) or path.name}: zip 包含符号链接 {info.filename}",
                    )
                if info.file_size > ARCHIVE_MAX_FILE_BYTES:
                    return (
                        [],
                        f"{resource_name(resource) or path.name}: zip 单文件超过限制 {info.filename}",
                    )
                total_size += int(info.file_size or 0)
                if total_size > ARCHIVE_MAX_TOTAL_BYTES:
                    return (
                        [],
                        f"{resource_name(resource) or path.name}: zip 总解压大小超过限制",
                    )
                target = _safe_archive_target(extract_root, info.filename)
                if target is None:
                    return (
                        [],
                        f"{resource_name(resource) or path.name}: zip 路径不安全 {info.filename}",
                    )
                if _is_archive_metadata(info.filename):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src:
                    material_store.copy_stream(target, src, expected_bytes=info.file_size, source={
                        'resource_id': resource.get('id') or resource.get('resource_id'), 'kind': 'archive_member',
                    })
                extracted.append(
                    {
                        "resource_id": f"{resource_key(resource)}::{index}",
                        "parent_resource_id": str(
                            resource.get("id") or resource.get("resource_id") or resource_key(resource)
                        ),
                        "name": f"{resource_name(resource) or path.name}::{info.filename}",
                        "path": str(target),
                        "archive_path": info.filename,
                    }
                )
    except Exception as err:
        return [], f"{resource_name(resource) or path.name}: zip 解压失败: {err}"
    return extracted, ""


def _rar_tool() -> str:
    return shutil.which("bsdtar") or shutil.which("7zz") or shutil.which("7z") or ""


def _archive_child_item(
    resource: Dict[str, Any], index: int, target: Path, member_name: str
) -> Dict[str, Any]:
    return {
        "resource_id": f"{resource_key(resource)}::{index}",
        "parent_resource_id": str(resource.get("id") or resource.get("resource_id") or resource_key(resource)),
        "name": f"{resource_name(resource) or target.name}::{member_name}",
        "path": str(target),
        "archive_path": member_name,
    }


def _kill_archive_process(process) -> None:
    if process.poll() is None:
        try:
            if getattr(process, 'pid', None):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass


def _rar_list_members(tool: str, path: Path) -> Tuple[List[Dict[str, Any]], str]:
    try:
        command = (
            [tool, "l", "-slt", "-ba", str(path)]
            if Path(tool).name.startswith("7z")
            else [tool, "-tvf", str(path), "--numeric-owner"]
        )
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        timer = threading.Timer(ARCHIVE_EXTRACT_TIMEOUT_SEC, _kill_archive_process, args=(process,))
        timer.start()
        try:
            with process.stdout as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                return [], 'rar 成员列表超过限制'
            if process.wait(timeout=ARCHIVE_EXTRACT_TIMEOUT_SEC) != 0:
                return [], 'rar 列表读取失败'
            output = raw.decode('utf-8')
        finally:
            timer.cancel()
            _kill_archive_process(process)
            process.wait()
        members = []
        if Path(tool).name.startswith("7z"):
            for block in re.split(r"\n\s*\n", output.strip()):
                fields = {}
                for line in block.splitlines():
                    key, separator, value = line.partition(" = ")
                    if not separator or key in fields:
                        raise ValueError("rar 成员元数据无效")
                    fields[key] = value
                if (not fields.get("Path") or fields.get("Folder") not in {"+", "-"}
                        or not fields.get("Size", "").isdigit()
                        or fields.get("Symbolic Link") or fields.get("Hard Link")
                        or fields.get("Anti") == "+"):
                    raise ValueError("rar 成员类型或元数据不可信")
                members.append({
                    "name": fields["Path"], "is_dir": fields["Folder"] == "+",
                    "size": int(fields["Size"]),
                })
        else:
            for line in output.splitlines():
                fields = line.split(maxsplit=8)
                if (len(fields) != 9 or len(fields[0]) != 10
                        or fields[0][0] not in {"-", "d"} or not fields[4].isdigit()):
                    raise ValueError("rar 成员类型或元数据不可信")
                members.append({
                    "name": fields[8], "is_dir": fields[0][0] == "d",
                    "size": int(fields[4]),
                })
        return members, ""
    except Exception as err:
        return [], str(err)


def _rar_extract(tool: str, path: Path, extract_root: Path, members: list[dict], resource: dict) -> str:
    for member in members:
        if member['is_dir']:
            continue
        target = _safe_archive_target(extract_root, member['name'])
        if target is None:
            return 'rar 路径不安全'
        command = ([tool, 'x', '-so', str(path), member['name']]
                   if Path(tool).name.startswith('7z') else [tool, '-xOf', str(path), member['name']])
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        timer = threading.Timer(ARCHIVE_EXTRACT_TIMEOUT_SEC, _kill_archive_process, args=(process,))
        timer.start()
        try:
            with process.stdout as source:
                material_store.copy_stream(target, source, expected_bytes=member['size'], source={
                    'resource_id': resource.get('id') or resource.get('resource_id'), 'kind': 'archive_member',
                })
            if process.wait(timeout=ARCHIVE_EXTRACT_TIMEOUT_SEC) != 0:
                return 'rar 成员读取失败'
        except Exception as error:
            return str(error) if str(error).startswith('material_') else 'rar 成员读取失败'
        finally:
            timer.cancel()
            _kill_archive_process(process)
            process.wait()
    return ''


def extract_rar_safely(
    path: Path, *, dest_dir: Path, resource: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], str]:
    tool = _rar_tool()
    if not tool:
        return (
            [],
            f"{resource_name(resource) or path.name}: rar 解压工具不可用，请改为 .zip 或展开为 PDF/图片后上传",
        )
    key = safe_filename(resource_key(resource), "archive")
    extract_root = dest_dir / EXTRACT_DIR_NAME / key
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    members, err = _rar_list_members(tool, path)
    if err:
        return [], f"{resource_name(resource) or path.name}: rar 列表读取失败: {err}"
    files = [member for member in members if not member["is_dir"]]
    if len(files) > ARCHIVE_MAX_FILES:
        return (
            [],
            f"{resource_name(resource) or path.name}: rar 文件数超过限制 {ARCHIVE_MAX_FILES}",
        )
    if len(members) > 2 * ARCHIVE_MAX_FILES:
        return [], f"{resource_name(resource) or path.name}: rar 成员数超过限制 {2 * ARCHIVE_MAX_FILES}"
    total_size = 0
    for member in members:
        member_name = member["name"]
        if _safe_archive_target(extract_root, member_name) is None:
            return (
                [],
                f"{resource_name(resource) or path.name}: rar 路径不安全 {member_name}",
            )
        if not member["is_dir"]:
            if member["size"] > ARCHIVE_MAX_FILE_BYTES:
                return [], f"{resource_name(resource) or path.name}: rar 单文件超过限制 {member_name}"
            total_size += member["size"]
            if total_size > ARCHIVE_MAX_TOTAL_BYTES:
                return [], f"{resource_name(resource) or path.name}: rar 总解压大小超过限制"
    err = _rar_extract(tool, path, extract_root, members, resource)
    if err:
        return [], f"{resource_name(resource) or path.name}: rar 解压失败: {err}"
    extracted: List[Dict[str, Any]] = []
    total_size = 0
    for index, member in enumerate(files, 1):
        member_name = member["name"]
        target = _safe_archive_target(extract_root, member_name)
        if target is None:
            return (
                [],
                f"{resource_name(resource) or path.name}: rar 路径不安全 {member_name}",
            )
        if not target.is_file():
            return [], f"{resource_name(resource) or path.name}: rar 成员文件缺失或类型不符 {member_name}"
        if target.is_symlink():
            return (
                [],
                f"{resource_name(resource) or path.name}: rar 包含符号链接 {member_name}",
            )
        size = target.stat().st_size
        if size != member["size"]:
            return [], f"{resource_name(resource) or path.name}: rar 成员文件大小不符 {member_name}"
        if size > ARCHIVE_MAX_FILE_BYTES:
            return (
                [],
                f"{resource_name(resource) or path.name}: rar 单文件超过限制 {member_name}",
            )
        total_size += size
        if total_size > ARCHIVE_MAX_TOTAL_BYTES:
            return [], f"{resource_name(resource) or path.name}: rar 总解压大小超过限制"
        if _is_archive_metadata(member_name):
            continue
        extracted.append(_archive_child_item(resource, index, target, member_name))
    return extracted, ""


def _extract_archive(
    path: Path, *, dest_dir: Path, resource: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], str]:
    return (
        extract_zip_safely(path, dest_dir=dest_dir, resource=resource)
        if path.suffix.lower() in ZIP_EXTS
        else extract_rar_safely(path, dest_dir=dest_dir, resource=resource)
    )


def _flatten_archive_children(
    dest_dir: Path,
    children: List[Dict[str, Any]],
    *,
    root_resource_id: str,
    depth: int,
) -> Tuple[List[Dict[str, Any]], str]:
    flattened: List[Dict[str, Any]] = []
    for child in children:
        path = Path(str(child.get("path") or ""))
        if path.suffix.lower() not in ZIP_EXTS | RAR_EXTS:
            leaf = dict(child)
            leaf["parent_resource_id"] = root_resource_id
            flattened.append(leaf)
        else:
            if depth >= ARCHIVE_MAX_DEPTH:
                return (
                    [],
                    f"{child.get('name') or path.name}: 压缩包嵌套层数超过限制 {ARCHIVE_MAX_DEPTH}",
                )
            nested_resource = {
                "id": str(child.get("resource_id") or ""),
                "name": str(child.get("name") or path.name),
            }
            nested_children, err = _extract_archive(
                path, dest_dir=dest_dir, resource=nested_resource
            )
            if err:
                return [], err
            nested_flattened, err = _flatten_archive_children(
                dest_dir,
                nested_children,
                root_resource_id=root_resource_id,
                depth=depth + 1,
            )
            if err:
                return [], err
            parent_archive_path = str(child.get("archive_path") or path.name)
            for nested_child in nested_flattened:
                nested_archive_path = str(nested_child.get("archive_path") or "")
                nested_child["archive_path"] = "::".join(
                    part for part in (parent_archive_path, nested_archive_path) if part
                )
            flattened.extend(nested_flattened)
        if len(flattened) > ARCHIVE_MAX_FILES:
            return [], f"压缩包展开后文件数超过限制 {ARCHIVE_MAX_FILES}"
        total_size = sum(
            Path(str(item.get("path") or "")).stat().st_size for item in flattened
        )
        if total_size > ARCHIVE_MAX_TOTAL_BYTES:
            return [], "压缩包展开后总大小超过限制"
    return flattened, ""


def _attach_archive_children(
    dest_dir: Path, resource: Dict[str, Any], item: Dict[str, Any]
) -> Tuple[Dict[str, Any] | None, str]:
    path = Path(str(item.get("path") or ""))
    suffix = path.suffix.lower()
    if suffix not in ZIP_EXTS | RAR_EXTS:
        return item, ""
    if item.get("archive_status") == "extracted" and isinstance(item.get("extracted_files"), list):
        children = item["extracted_files"]
        if all(Path(str(child.get("path") or "")).is_file()
               and not Path(str(child.get("path") or "")).is_symlink() for child in children):
            return item, ""
        return None, "attachment_archive_cached_children_invalid"
    extracted, err = _extract_archive(path, dest_dir=dest_dir, resource=resource)
    if err:
        return None, err
    extracted, err = _flatten_archive_children(
        dest_dir,
        extracted,
        root_resource_id=str(resource.get("id") or resource.get("resource_id") or resource_key(resource)),
        depth=1,
    )
    if err:
        return None, err
    item["archive_status"] = "extracted"
    item["archive_extracted_count"] = len(extracted)
    item["extracted_files"] = extracted
    return item, ""
