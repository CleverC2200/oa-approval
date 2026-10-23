from __future__ import annotations


def image_locations(pages):
    numbers = {}
    for page in pages:
        resource_id = str(page.get('parent_resource_id') or page.get('resource_id') or '')
        numbers[resource_id] = numbers.get(resource_id, 0) + 1
        yield {'resource_id': resource_id, 'page_number': numbers[resource_id],
               'evidence_ref': f'material:prepared.images.{resource_id}.{numbers[resource_id]}'}, page
