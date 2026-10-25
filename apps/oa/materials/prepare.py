from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from apps import json_artifact


def _source_hash(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 512 * 1024 * 1024:
            raise ValueError('material_source_invalid')
        digest = hashlib.sha256()
        size = 0
        for block in iter(lambda: source.read(1024 * 1024), b''):
            size += len(block)
            if size > 512 * 1024 * 1024:
                raise ValueError('material_source_invalid')
            digest.update(block)
        after = os.fstat(source.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('material_source_changed')
    return digest.hexdigest()


def _record(value, *, base: Path, fallback: str, parent: str | None = None):
    if not isinstance(value, dict) or not isinstance(value.get('path'), str):
        raise ValueError('material_prepare_input_invalid')
    path = Path(value['path']).expanduser()
    path = (base / path).absolute() if not path.is_absolute() else path.absolute()
    resource_id = str(value.get('resource_id') or value.get('id') or value.get('resourceId') or value.get('fileId') or fallback)
    if not resource_id.strip() or parent is not None and str(value.get('parent_resource_id') or parent) != parent:
        raise ValueError('material_resource_identity_conflict')
    item = {'resource_id': resource_id, 'path': str(path), 'name': str(value.get('name') or path.name),
            'source': 'local', 'content_sha256': _source_hash(path)}
    if parent is not None:
        item['parent_resource_id'] = parent
    elif value.get('parent_resource_id'):
        item['parent_resource_id'] = str(value['parent_resource_id'])
    if value.get('archive_path'):
        item['archive_path'] = str(value['archive_path'])
    children = value.get('extracted_files')
    if children is not None:
        if not isinstance(children, list) or not children:
            raise ValueError('material_archive_invalid')
        item.update(archive_status='extracted', archive_extracted_count=len(children), extracted_files=[
            _record(child, base=base, fallback=f'{resource_id}::{index}', parent=resource_id)
            for index, child in enumerate(children, 1)])
    return item


def _inputs(files, manifest):
    if bool(files) == bool(manifest):
        raise ValueError('material_prepare_input_invalid')
    if manifest is None:
        values = [{'path': str(path)} for path in files]
        base = Path.cwd()
    else:
        manifest = Path(manifest).expanduser().absolute()
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError('material_prepare_input_invalid')
        raw = json.loads(manifest.read_text())
        values = raw.get('resources') if isinstance(raw, dict) else raw
        if not isinstance(values, list) or not values:
            raise ValueError('material_prepare_input_invalid')
        base = manifest.parent
    return [_record(value, base=base, fallback=f'local-{index}') for index, value in enumerate(values, 1)]


def _sources(resources):
    for item in resources:
        yield item
        yield from _sources(item.get('extracted_files') or [])


def prepare(*, files=None, manifest=None, output_dir: Path):
    from apps.oa.materials import archives, formats

    resources = _inputs(files, manifest)
    sources = {item['path']: item['content_sha256'] for item in _sources(resources)}
    output = Path(output_dir).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('material_output_exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    material_file = output / 'material.json'
    artifact = {'schema_version': 1, 'kind': 'local_material', 'status': 'preparing', 'resources': resources,
                'prepared': {'images': [], 'manifest': [], 'structured_contents': []}}
    try:
        for index, item in enumerate(resources, 1):
            expanded, error = archives._attach_archive_children(output / 'archives' / str(index), item, item)
            if error or expanded is None:
                raise ValueError('material_archive_invalid')
            resources[index - 1] = _record(expanded, base=Path.cwd(), fallback=item['resource_id'])
            for source in _sources([resources[index - 1]]):
                if sources.setdefault(source['path'], source['content_sha256']) != source['content_sha256']:
                    raise ValueError('material_source_changed')
        identities = {}
        for item in _sources(resources):
            key = formats.safe_name(item['resource_id'], 'resource')
            if key in identities:
                raise ValueError('material_resource_identity_conflict')
            identities[key] = item['resource_id']
        artifact['prepared'] = formats.prepare_materials(resources, out_dir=output / 'prepared').to_record()
        for path, digest in sources.items():
            if _source_hash(path) != digest:
                raise ValueError('material_source_changed')
        artifact['status'] = 'complete'
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        code = str(error)
        artifact.update(status='failed', error_code=code if code.startswith(('material_', 'attachment_'))
                        else 'material_prepare_failed')
    json_artifact.write_json_atomic(material_file, artifact, indent=2)
    return {'status': artifact['status'], 'material_file': str(material_file), 'resources': len(resources),
            'images': len(artifact['prepared']['images']),
            'structured_contents': len(artifact['prepared']['structured_contents']),
            **({'error_code': artifact['error_code']} if 'error_code' in artifact else {})}
