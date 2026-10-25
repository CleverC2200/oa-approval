from __future__ import annotations

import re

from apps.oa.materials.resources import image_locations


def _records(record: dict, key: str) -> list:
    values = record.get(key, [])
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise ValueError('material_file_invalid')
    return values


def _location(section: str, index: int) -> dict:
    return {'pointer': '/' + section.replace('.', '/') + f'/{index}',
            'text_section': f'material.{section}', 'text_index': index}


def _resources(resources: list, pointer: str = '/resources', parent: str | None = None):
    for index, resource in enumerate(resources):
        location = f'{pointer}/{index}'
        yield location, resource, resource.get('parent_resource_id') or parent
        yield from _resources(_records(resource, 'extracted_files'), f'{location}/extracted_files',
                              resource.get('resource_id'))


def _reuse(resource_entries: list, prepared: dict) -> dict:
    sources = {}
    paths = {}
    for _, resource, parent in resource_entries:
        digest = resource.get('source_content_sha256') or resource.get('content_sha256')
        if digest:
            sources.setdefault(digest, {})[(resource.get('resource_id'), parent)] = None
    for section in ('images', 'structured_contents'):
        for index, item in enumerate(prepared.get(section) or []):
            digest = item.get('source_content_sha256')
            if digest:
                sources.setdefault(digest, {})[(item.get('resource_id'), item.get('parent_resource_id'))] = None
            if item.get('path'):
                paths.setdefault(item['path'], []).append(f'/prepared/{section}/{index}')
    return {
        'meaning': 'Matching source hashes and shared prepared paths only; no logical resources are removed.',
        'matching_source_content': [
            {'content_sha256': digest, 'sources': [
                {'resource_id': resource_id, 'parent_resource_id': parent} for resource_id, parent in identities]}
            for digest, identities in sources.items() if len(identities) > 1],
        'shared_prepared_paths': [{'path': path, 'pointers': pointers}
                                  for path, pointers in paths.items() if len(pointers) > 1],
    }


def outline(material: dict) -> dict:
    fields = _records(material, 'declared_fields')
    resources = _records(material, 'resources')
    prepared = material.get('prepared', {})
    if not isinstance(prepared, dict):
        raise ValueError('material_file_invalid')
    images = _records(prepared, 'images')
    structured = _records(prepared, 'structured_contents')
    resource_entries = list(_resources(resources))
    tables = {}
    for field in fields:
        match = re.fullmatch(r'(detail\[[0-9]+\])\.row\[([0-9]+)\]\.field\[[0-9]+\]', str(field.get('path') or ''))
        if match:
            table = tables.setdefault(match[1], {'rows': set(), 'field_count': 0})
            table['rows'].add(match[2])
            table['field_count'] += 1
    return {
        'kind': 'material_outline', 'coverage': 'navigation_only',
        'instructions': 'Pointers locate the frozen JSON; text_section and text_index locate the complete read output. '
                        'Counts and navigation do not count as reading coverage. Read complete frozen material and rules.',
        'identity': {key: material[key] for key in ('schema_version', 'kind', 'request_id', 'workflow_key')
                     if key in material},
        'counts': {'declared_fields': len(fields), 'detail_tables': len(tables), 'root_resources': len(resources),
                   'logical_resources': len(resource_entries),
                   'archive_child_resources': len(resource_entries) - len(resources),
                   'prepared_images': len(images), 'structured_contents': len(structured)},
        'detail_tables': [{'field_path': path, 'row_count': len(table['rows']), 'field_count': table['field_count']}
                          for path, table in tables.items()],
        'content_reuse': _reuse(resource_entries, prepared),
        'navigation': {
            'fields': [_location('declared_fields', index) | {'field_path': field.get('path')}
                       for index, field in enumerate(fields)],
            'resources': [{'pointer': pointer, 'resource_id': resource.get('resource_id'),
                           'parent_resource_id': parent, 'owner_detail_id': resource.get('owner_detail_id'),
                           'source_field_path': resource.get('source_field_path'),
                           'original_path': resource.get('local_readonly_path') or resource.get('path'),
                           **({'archive_path': resource['archive_path']} if 'archive_path' in resource else {})}
                          for pointer, resource, parent in resource_entries],
            'images': [_location('prepared.images', index) | {
                'resource_id': page.get('resource_id'), 'parent_resource_id': page.get('parent_resource_id'),
                'page_number': location['page_number'], 'evidence_ref': location['evidence_ref'],
                'path': page.get('path'),
            } for index, (location, page) in enumerate(image_locations(images))],
            'structured_contents': [_location('prepared.structured_contents', index) | {
                'resource_id': content.get('resource_id'), 'parent_resource_id': content.get('parent_resource_id'),
            } for index, content in enumerate(structured)],
        },
    }
