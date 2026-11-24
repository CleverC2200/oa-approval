from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from zlib import crc32

from apps.oa.runtime.files import MAX_FILE_BYTES


PART_NAME = re.compile(r'(.+)\.part([0-9]+)\.rar', re.IGNORECASE)
LEGACY_PART_NAME = re.compile(r'.+\.[r-z][0-9]{2}', re.IGNORECASE)
RAR5_SIGNATURE = b'Rar!\x1a\x07\x01\x00'
RAR4_SIGNATURE = b'Rar!\x1a\x07\x00'
HEADER_EXTRA = 0x01
HEADER_DATA = 0x02
SPLIT_BEFORE = 0x08
SPLIT_AFTER = 0x10
MAIN_HEADER = 1
FILE_HEADER = 2
ENCRYPTION_HEADER = 4
END_HEADER = 5
DIRECTORY_FLAG = 0x01
VOLUME_FLAG = 0x01
VOLUME_NUMBER_FLAG = 0x02
MORE_VOLUMES_FLAG = 0x01
MAX_HEADER_BYTES = 4 * 1024 * 1024


class VolumeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Volume:
    number: int
    has_next: bool
    split_before: bool
    split_after: bool
    size: int
    file_sizes: tuple[int, ...]


def _vint(read_byte, limit=10):
    encoded = bytearray()
    for _ in range(limit):
        byte = read_byte()
        if not byte:
            break
        encoded.extend(byte)
        if byte[0] < 128:
            value = sum((digit & 127) << (index * 7) for index, digit in enumerate(encoded))
            if value < 1 << 64:
                return value, encoded
            break
    raise VolumeError('rar_volume_header_invalid')


def _header(stream, remaining):
    checksum = stream.read(4)
    length, encoded_length = _vint(lambda: stream.read(1), limit=3)
    if length < 2 or length + len(encoded_length) + 4 > remaining:
        raise VolumeError('rar_volume_header_limit')
    data = stream.read(length)
    if len(checksum) != 4 or len(data) != length or crc32(encoded_length + data) != int.from_bytes(checksum, 'little'):
        raise VolumeError('rar_volume_header_invalid')
    return data


def _fields(header):
    cursor = iter(header)

    def next_integer():
        return _vint(lambda: bytes([digit]) if (digit := next(cursor, None)) is not None else b'')[0]

    kind, flags = next_integer(), next_integer()
    if flags & HEADER_EXTRA:
        next_integer()
    data_size = next_integer() if flags & HEADER_DATA else 0
    return kind, flags, data_size, next_integer


def inspect(path: Path, *, named_part: bool) -> Volume | None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
            raise VolumeError('rar_volume_input_limit')
        magic = stream.read(len(RAR5_SIGNATURE))
        if magic != RAR5_SIGNATURE:
            if magic.startswith(RAR4_SIGNATURE):
                main = magic[len(RAR4_SIGNATURE):] + stream.read(6)
                if len(main) == 7 and main[2] == 0x73:
                    if main[3] & VOLUME_FLAG:
                        raise VolumeError('rar_volume_format_unsupported')
                    return None
            raise VolumeError('rar_volume_header_invalid')
        first = _header(stream, MAX_HEADER_BYTES)
        kind, _, data_size, next_integer = _fields(first)
        if kind == ENCRYPTION_HEADER and not named_part:
            return None
        if kind != MAIN_HEADER or data_size:
            raise VolumeError('rar_volume_header_invalid')
        flags = next_integer()
        if not flags & VOLUME_FLAG:
            return None
        if not named_part:
            raise VolumeError('rar_volume_name_invalid')
        number = 1 + (next_integer() if flags & VOLUME_NUMBER_FLAG else 0)
        split_before = split_after = False
        file_sizes = []
        header_bytes = len(first) + 7
        while stream.tell() < metadata.st_size:
            header = _header(stream, MAX_HEADER_BYTES - header_bytes)
            header_bytes += len(header) + 7
            kind, flags, data_size, next_integer = _fields(header)
            if kind == FILE_HEADER:
                if next_integer() & DIRECTORY_FLAG:
                    if data_size or flags & (SPLIT_BEFORE | SPLIT_AFTER):
                        raise VolumeError('rar_volume_header_invalid')
                else:
                    file_sizes.append(data_size)
                    split_before |= bool(flags & SPLIT_BEFORE)
                    split_after |= bool(flags & SPLIT_AFTER)
            if stream.tell() + data_size > metadata.st_size:
                raise VolumeError('rar_volume_header_invalid')
            if kind == END_HEADER:
                padding = metadata.st_size - stream.tell()
                if data_size or padding > MAX_HEADER_BYTES or any(stream.read(padding)):
                    raise VolumeError('rar_volume_header_invalid')
                return Volume(number, bool(next_integer() & MORE_VOLUMES_FLAG),
                              split_before, split_after, metadata.st_size, tuple(file_sizes))
            stream.seek(data_size, os.SEEK_CUR)
        raise VolumeError('rar_volume_header_invalid')
