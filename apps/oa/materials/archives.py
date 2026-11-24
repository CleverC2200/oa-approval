from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
from tempfile import TemporaryDirectory
import threading
from time import monotonic
from typing import Any, Dict, List, Tuple
import zipfile
from uuid import uuid4

from apps.oa.materials import rar_volumes
from apps.oa.materials.resources import resource_name, resource_key, safe_filename
from apps.oa.runtime import files as material_store


EXTRACT_DIR_NAME = "extracted"
ZIP_EXTS = {".zip"}
RAR_EXTS = {".rar"}
ARCHIVE_MAX_FILES = int(os.environ.get("OA_ATTACHMENT_ARCHIVE_MAX_FILES", "256"))
ARCHIVE_MAX_FILE_BYTES = int(
    os.environ.get("OA_ATTACHMENT_ARCHIVE_MAX_FILE_BYTES", str(50 * 1024 * 1024))
)
ARCHIVE_MAX_TOTAL_BYTES = int(
    os.environ.get("OA_ATTACHMENT_ARCHIVE_MAX_TOTAL_BYTES", str(200 * 1024 * 1024))
)
RAR_VOLUME_MAX_FILE_BYTES = 160 * 1024 * 1024
RAR_VOLUME_MAX_TOTAL_BYTES = 200 * 1024 * 1024
ARCHIVE_MAX_DEPTH = 3
ARCHIVE_EXTRACT_TIMEOUT_SEC = int(
    os.environ.get("OA_ATTACHMENT_ARCHIVE_EXTRACT_TIMEOUT_SEC", "30")
)


@dataclass(slots=True)
class _RarVolumeBudget:
    remaining_bytes: int
    deadline: float

    def check_deadline(self) -> None:
        if monotonic() >= self.deadline:
            raise rar_volumes.VolumeError('rar_archive_timeout')

    def reserve(self, byte_count: int) -> None:
        self.check_deadline()
        if byte_count > self.remaining_bytes:
            raise rar_volumes.VolumeError('rar_archive_total_size_limit')
        self.remaining_bytes -= byte_count


@dataclass(frozen=True, slots=True)
class _ArchiveDeadlineReader:
    source: zipfile.ZipExtFile
    budget: _RarVolumeBudget

    def read(self, size: int) -> bytes:
        self.budget.check_deadline()
        return self.source.read(size)


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def _is_archive_metadata(member_name: str) -> bool:
    parts = [part for part in str(member_name or "").split("/") if part]
    return "__MACOSX" in parts or ".DS_Store" in parts or any(part.startswith("._") for part in parts)


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
    path: Path, *, dest_dir: Path, resource: Dict[str, Any], budget: _RarVolumeBudget | None = None,
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
                    f"archive_file_limit: {resource_name(resource) or path.name}: zip 文件数超过限制 {ARCHIVE_MAX_FILES}",
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
                if budget is not None:
                    budget.reserve(info.file_size)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src:
                    stream = src if budget is None else _ArchiveDeadlineReader(src, budget)
                    material_store.copy_stream(target, stream, expected_bytes=info.file_size, source={
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
    except rar_volumes.VolumeError as error:
        return [], str(error)
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


@contextmanager
def _rar_output(command, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise rar_volumes.VolumeError('rar_archive_timeout')
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    expired = threading.Event()

    def expire():
        expired.set()
        _kill_archive_process(process)

    timer = threading.Timer(remaining, expire)
    timer.start()
    try:
        with process.stdout as stream:
            yield stream
        if process.wait(timeout=max(0, deadline - monotonic())) != 0:
            raise rar_volumes.VolumeError('rar_backend_failed')
    except subprocess.TimeoutExpired:
        raise rar_volumes.VolumeError('rar_archive_timeout') from None
    finally:
        timer.cancel()
        _kill_archive_process(process)
        process.wait()
        if expired.is_set():
            raise rar_volumes.VolumeError('rar_archive_timeout')


def _rar_listing(command, deadline):
    with _rar_output(command, deadline) as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise rar_volumes.VolumeError('rar_listing_limit')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise rar_volumes.VolumeError('rar_listing_invalid') from None


def _rar_metadata(block):
    fields = {}
    for line in block.splitlines():
        key, separator, value = line.partition(" = ")
        if not separator or key in fields:
            raise rar_volumes.VolumeError('rar_listing_invalid')
        fields[key] = value
    return fields


def _sevenzip_members(output, *, require_checksum=False):
    members = []
    for block in re.split(r"\n\n+", output.strip("\r\n")) if output.strip() else []:
        fields = _rar_metadata(block)
        if (not fields.get("Path") or fields.get("Folder") not in {"+", "-"}
                or not fields.get("Size", "").isdigit()
                or any(fields.get(key) for key in ("Symbolic Link", "Hard Link", "Copy Link"))
                or any(fields.get(key) == "+" for key in ("Anti", "Alternate Stream", "Encrypted",
                                                         "Split Before", "Split After"))):
            raise rar_volumes.VolumeError('rar_archive_member_invalid')
        if (require_checksum and fields["Folder"] == "-" and not (
                re.fullmatch(r"[0-9A-Fa-f]{8}", fields.get("CRC", ""))
                or re.fullmatch(r"[0-9A-Fa-f]{64}", fields.get("Checksum", "")))):
            raise rar_volumes.VolumeError('rar_volume_checksum_unverified')
        members.append({"name": fields["Path"], "is_dir": fields["Folder"] == "+",
                        "size": int(fields["Size"])})
    return members


def _rar_list_members(tool, path, deadline):
    if Path(tool).name.startswith("7z"):
        return _sevenzip_members(_rar_listing([tool, "l", "-slt", "-ba", str(path)], deadline))
    output = _rar_listing([tool, "-tvf", str(path), "--numeric-owner"], deadline)
    members = []
    for line in output.splitlines():
        fields = line.split(maxsplit=8)
        if (len(fields) != 9 or len(fields[0]) != 10
                or fields[0][0] not in {"-", "d"} or not fields[4].isdigit()):
            raise rar_volumes.VolumeError('rar_archive_member_invalid')
        members.append({"name": fields[8], "is_dir": fields[0][0] == "d", "size": int(fields[4])})
    return members


def _rar_extract(tool, path, extract_root, members, resource, *, deadline=None):
    for member in members:
        if member['is_dir']:
            continue
        target = _safe_archive_target(extract_root, member['name'])
        command = ([tool, 'x', '-so', '-spd', '-p-', '-bd', '-i!' + member['name'], '--', str(path)]
                   if Path(tool).name.startswith('7z') else [tool, '-xOf', str(path), member['name']])
        try:
            with _rar_output(command, deadline if deadline is not None else monotonic() + ARCHIVE_EXTRACT_TIMEOUT_SEC) as source:
                material_store.copy_stream(target, source, expected_bytes=member['size'], source={
                    'resource_id': resource.get('id') or resource.get('resource_id'), 'kind': 'archive_member',
                })
        except ValueError as error:
            if str(error) == 'attachment_content_incomplete':
                raise rar_volumes.VolumeError('rar_archive_output_mismatch') from None
            raise
        except OSError:
            raise rar_volumes.VolumeError('rar_archive_extract_failed') from None


def _rar_member_limit(members, root, *, max_file, max_total, budget=None):
    if sum(not member["is_dir"] for member in members) > ARCHIVE_MAX_FILES:
        return 'archive_file_limit'
    if len(members) > 2 * ARCHIVE_MAX_FILES:
        return 'rar_archive_member_count_limit'
    paths = set()
    total = 0
    for member in members:
        name = member['name']
        target = _safe_archive_target(root, name)
        if (target is None or target == root or '..' in name.split('/')
                or any(ord(char) < 32 for char in name) or target in paths):
            return 'rar_archive_path_invalid'
        paths.add(target)
        if member['is_dir']:
            continue
        if member['size'] > max_file:
            return 'rar_archive_member_size_limit'
        total += member['size']
        if total > max_total:
            return 'rar_archive_total_size_limit'
    if budget is not None:
        budget.reserve(total)
    return ''


def _rar_children(root, members, resource):
    extracted = []
    for index, member in enumerate((member for member in members if not member['is_dir']), 1):
        name = member['name']
        target = _safe_archive_target(root, name)
        if target is None or target.is_symlink() or not target.is_file() or target.stat().st_size != member['size']:
            raise rar_volumes.VolumeError('rar_archive_output_mismatch')
        if not _is_archive_metadata(name):
            extracted.append(_archive_child_item(resource, index, target, name))
    return extracted


def extract_rar_safely(
    path: Path, *, dest_dir: Path, resource: Dict[str, Any], budget: _RarVolumeBudget | None = None,
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
    try:
        deadline = budget.deadline if budget is not None else monotonic() + ARCHIVE_EXTRACT_TIMEOUT_SEC
        members = _rar_list_members(tool, path, deadline)
        if failure := _rar_member_limit(members, extract_root, max_file=ARCHIVE_MAX_FILE_BYTES,
                                        max_total=ARCHIVE_MAX_TOTAL_BYTES, budget=budget):
            return [], f"{failure}: {resource_name(resource) or path.name}"
        _rar_extract(tool, path, extract_root, members, resource,
                     deadline=deadline if budget is not None else None)
        return _rar_children(extract_root, members, resource), ""
    except (OSError, ValueError) as error:
        if budget is not None and isinstance(error, rar_volumes.VolumeError):
            return [], str(error)
        return [], f"{resource_name(resource) or path.name}: rar 解包失败: {error}"


def _extract_archive(
    path: Path, *, dest_dir: Path, resource: Dict[str, Any], budget: _RarVolumeBudget | None = None,
) -> Tuple[List[Dict[str, Any]], str]:
    return (
        extract_zip_safely(path, dest_dir=dest_dir, resource=resource, budget=budget)
        if path.suffix.lower() in ZIP_EXTS
        else extract_rar_safely(path, dest_dir=dest_dir, resource=resource, budget=budget)
    )


def _flatten_archive_children(
    dest_dir: Path,
    children: List[Dict[str, Any]],
    *,
    root_resource_id: str,
    depth: int,
    budget: _RarVolumeBudget | None = None,
) -> Tuple[List[Dict[str, Any]], str]:
    flattened: List[Dict[str, Any]] = []
    for child in children:
        if budget is not None:
            budget.check_deadline()
        path = Path(str(child.get("path") or ""))
        if rar_volumes.LEGACY_PART_NAME.fullmatch(path.name):
            return [], 'rar_volume_nested_unsupported'
        if path.suffix.lower() in RAR_EXTS:
            try:
                if rar_volumes.inspect(path, named_part=bool(rar_volumes.PART_NAME.fullmatch(path.name))):
                    return [], 'rar_volume_nested_unsupported'
            except rar_volumes.VolumeError as error:
                return [], str(error)
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
                path, dest_dir=dest_dir, resource=nested_resource, budget=budget,
            )
            if err:
                return [], err
            nested_flattened, err = _flatten_archive_children(
                dest_dir,
                nested_children,
                root_resource_id=root_resource_id,
                depth=depth + 1,
                budget=budget,
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
            return [], f"archive_file_limit: 压缩包展开后文件数超过限制 {ARCHIVE_MAX_FILES}"
        if budget is None:
            total_size = sum(Path(item["path"]).stat().st_size for item in flattened)
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
        dest_dir = dest_dir / "expanded" / uuid4().hex
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


@dataclass(frozen=True, slots=True)
class ArchiveAttachment:
    resource: dict
    item: dict
    output: Path
    volume_source: tuple[str, ...] | None
    original_name: str = ""


@dataclass(frozen=True, slots=True)
class ArchiveFailure:
    resource_id: str
    reason: str
    message: str
    include_resource_ref: bool = True

    def diagnostic(self) -> dict:
        return {'stage': 'archive_expand', 'reason': self.reason,
                **({'attachment_ref': self.resource_id} if self.include_resource_ref and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', self.resource_id) else {})}


def _verified_rar_members(tool, path, *, volumes, physical_size, deadline):
    listing = _rar_listing([tool, 'l', '-slt', '-sccUTF-8', '-tRar5', '-p-', '--', str(path)], deadline)
    _, detail_separator, details = listing.partition('\n--\n')
    header, member_separator, contents = details.partition('\n----------\n')
    if not detail_separator or not member_separator:
        raise rar_volumes.VolumeError('rar_listing_invalid')
    fields = _rar_metadata(header.strip('\r\n'))
    if (fields.get('Type') != 'Rar5' or fields.get('Multivolume') != '+' or fields.get('Volume Index') != '0'
            or fields.get('Volumes') != str(volumes) or fields.get('Total Physical Size') != str(physical_size)):
        raise rar_volumes.VolumeError('rar_volume_group_mismatch')
    return _sevenzip_members(contents, require_checksum=True)


def _stage_volume(path, output, size):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise rar_volumes.VolumeError('rar_volume_source_changed')
        material_store.copy_stream(output, source, expected_bytes=size)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise rar_volumes.VolumeError('rar_volume_source_changed')


def _expand_volume_group(group):
    entries = sorted(group, key=lambda entry: entry[1].number)
    ordered = [volume for _, volume in entries]
    if len({volume.number for volume in ordered}) != len(ordered):
        raise rar_volumes.VolumeError('rar_volume_group_mismatch')
    if [volume.number for volume in ordered] != list(range(1, len(ordered) + 1)) or ordered[-1].has_next:
        raise rar_volumes.VolumeError('rar_volume_missing')
    if len(ordered) < 2 or any(not volume.has_next for volume in ordered[:-1]):
        raise rar_volumes.VolumeError('rar_volume_group_mismatch')
    if any(volume.split_before != bool(index) or volume.split_after != (index < len(ordered) - 1)
           for index, volume in enumerate(ordered)):
        raise rar_volumes.VolumeError('rar_volume_group_unverified')
    physical_size = sum(volume.size for volume in ordered)
    if len(ordered) > ARCHIVE_MAX_FILES or physical_size > material_store.MAX_FILE_BYTES:
        raise rar_volumes.VolumeError('rar_volume_input_limit')
    tool = shutil.which('7zz')
    if tool is None:
        raise rar_volumes.VolumeError('rar_volume_7zz_unavailable')
    first = entries[0][0]
    group_root = first.output / EXTRACT_DIR_NAME / uuid4().hex
    group_root.mkdir(parents=True, mode=0o700)
    try:
        children = _extract_volume_group(tool, group_root, entries, physical_size=physical_size)
    except (OSError, ValueError):
        shutil.rmtree(group_root, ignore_errors=True)
        raise
    volume_set = {'backend': '7zz', 'format': 'Rar5',
                  'volume_resource_ids': [entry.item['resource_id'] for entry, _ in entries]}
    for entry, _ in entries:
        entry.item['archive_volume_set'] = volume_set
        entry.item.pop('extracted_files', None)
        entry.item.pop('archive_extracted_count', None)
        entry.item['archive_status'] = 'volume_verified'
    first.item.update(archive_status='extracted', archive_extracted_count=len(children), extracted_files=children)


def _extract_volume_group(tool, group_root, entries, *, physical_size):
    first = entries[0][0]
    extract_root = group_root / 'members'
    extract_root.mkdir(mode=0o700)
    width = max(3, len(str(len(entries))))
    with TemporaryDirectory(prefix='rar-volumes-', dir=group_root) as directory:
        for entry, volume in entries:
            staged = Path(directory) / f'evidence.part{volume.number:0{width}d}.rar'
            _stage_volume(Path(entry.item['path']), staged, volume.size)
            if rar_volumes.inspect(staged, named_part=True) != volume:
                raise rar_volumes.VolumeError('rar_volume_source_changed')
        start = Path(directory) / f'evidence.part{1:0{width}d}.rar'
        deadline = monotonic() + ARCHIVE_EXTRACT_TIMEOUT_SEC
        budget = _RarVolumeBudget(RAR_VOLUME_MAX_TOTAL_BYTES, deadline)
        try:
            members = _verified_rar_members(tool, start, volumes=len(entries),
                                            physical_size=physical_size, deadline=deadline)
        except rar_volumes.VolumeError as error:
            if str(error) == 'rar_backend_failed':
                raise rar_volumes.VolumeError('rar_volume_listing_failed') from None
            raise
        if failure := _rar_member_limit(members, extract_root, max_file=RAR_VOLUME_MAX_FILE_BYTES,
                                        max_total=RAR_VOLUME_MAX_TOTAL_BYTES, budget=budget):
            raise rar_volumes.VolumeError(failure)
        if sum(not member['is_dir'] for member in members) != 1 or any(len(volume.file_sizes) != 1 for _, volume in entries):
            raise rar_volumes.VolumeError('rar_volume_multiple_members_unverified')
        if any(not volume.file_sizes[0] for _, volume in entries):
            raise rar_volumes.VolumeError('rar_volume_group_unverified')
        try:
            with _rar_output([tool, 't', '-tRar5', '-p-', '-bd', '-bso0', '-bse0', '-bsp0',
                              '--', str(start)], deadline) as output:
                if output.read(1):
                    raise rar_volumes.VolumeError('rar_volume_integrity_output_invalid')
        except rar_volumes.VolumeError as error:
            if str(error) == 'rar_backend_failed':
                raise rar_volumes.VolumeError('rar_volume_integrity_failed') from None
            raise
        try:
            _rar_extract(tool, start, extract_root, members, first.resource, deadline=deadline)
        except rar_volumes.VolumeError as error:
            if str(error) == 'rar_backend_failed':
                raise rar_volumes.VolumeError('rar_archive_extract_failed') from None
            raise
    children, error = _flatten_archive_children(
        group_root, _rar_children(extract_root, members, first.resource),
        root_resource_id=first.item['resource_id'], depth=1, budget=budget,
    )
    if error:
        if error.startswith('archive_file_limit:'):
            raise rar_volumes.VolumeError('archive_file_limit')
        raise rar_volumes.VolumeError(error if re.fullmatch(r'rar_[a-z_]+', error) else 'rar_archive_nested_failed')
    if not children:
        raise rar_volumes.VolumeError('rar_archive_members_empty')
    budget.check_deadline()
    return children


def expand_attachment_archives(attachments: list[ArchiveAttachment]) -> list[ArchiveFailure]:
    failures = []
    groups = {}
    for entry in attachments:
        path = Path(entry.item['path'])
        name = entry.original_name or resource_name(entry.resource) or path.name
        part = rar_volumes.PART_NAME.fullmatch(name)
        if part or path.suffix.lower() in RAR_EXTS or Path(name).suffix.lower() in RAR_EXTS:
            try:
                volume = rar_volumes.inspect(path, named_part=part is not None)
                if volume is not None:
                    if len(part[2]) > 6 or int(part[2]) != volume.number:
                        raise rar_volumes.VolumeError('rar_volume_group_mismatch')
                    if entry.volume_source is None:
                        raise rar_volumes.VolumeError('rar_volume_source_unverified')
                    groups.setdefault((entry.volume_source, part[1]), []).append((entry, volume))
                    continue
            except (OSError, rar_volumes.VolumeError) as error:
                reason = str(error) if isinstance(error, rar_volumes.VolumeError) else 'rar_volume_input_unreadable'
                failures.append(ArchiveFailure(entry.item['resource_id'], reason, reason))
                continue
        elif rar_volumes.LEGACY_PART_NAME.fullmatch(name):
            failures.append(ArchiveFailure(entry.item['resource_id'], 'rar_volume_format_unsupported',
                                           'rar_volume_format_unsupported'))
            continue
        _, error = _attach_archive_children(entry.output, entry.resource, entry.item)
        if error:
            reason = 'archive_file_limit' if error.startswith('archive_file_limit: ') else 'archive_expansion_failed'
            failures.append(ArchiveFailure(entry.item['resource_id'], reason, error, include_resource_ref=False))
    for group in groups.values():
        try:
            _expand_volume_group(group)
        except (OSError, ValueError) as error:
            reason = str(error) if isinstance(error, rar_volumes.VolumeError) else 'rar_archive_expand_failed'
            failures.extend(ArchiveFailure(entry.item['resource_id'], reason, reason) for entry, _ in group)
    return failures
