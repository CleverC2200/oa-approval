from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile


MAX_FILE_BYTES = 512 * 1024 * 1024


def copy_stream(path: Path, stream, *, expected_bytes: int | None = None,
                source: dict | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        size = 0
        with os.fdopen(descriptor, 'wb') as output:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ValueError('attachment_file_too_large')
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if expected_bytes is not None and size != expected_bytes:
            raise ValueError('attachment_content_incomplete')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def write_bytes(path: Path, data: bytes, *, source: dict | None = None) -> Path:
    from io import BytesIO

    return copy_stream(path, BytesIO(data), expected_bytes=len(data))


@contextmanager
def bounded_output(path: Path, *, max_bytes: int):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        raise ValueError('attachment_output_exists')
    yield path
    if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
        raise ValueError('attachment_output_invalid')
