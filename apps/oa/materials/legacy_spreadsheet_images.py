from __future__ import annotations

from collections import Counter
from io import StringIO
from pathlib import Path
import struct

from xlrd.compdoc import CompDoc
from openpyxl.utils import get_column_letter


def _records(data, *, office=False):
    data = memoryview(data)
    position = 0
    header = 8 if office else 4
    while position < len(data):
        if position + header > len(data):
            raise ValueError('legacy_spreadsheet_record_truncated')
        if office:
            options, kind, length = struct.unpack_from('<HHI', data, position)
        else:
            kind, length = struct.unpack_from('<HH', data, position)
            options = 0
        end = position + header + length
        if end > len(data):
            raise ValueError('legacy_spreadsheet_record_truncated')
        yield position, kind, options, data[position + header:end]
        position = end


def _substream(workbook, start, stream_type):
    records = []
    for record in _records(memoryview(workbook)[start:]):
        records.append(record)
        if record[1] == 0xa:
            if record[3]:
                raise ValueError('legacy_spreadsheet_stream_incomplete')
            break
    if (not records or records[-1][1] != 0xa or records[0][1] != 0x809
            or records[0][3][:4] != struct.pack('<HH', 0x600, stream_type)
            or any(record[1] == 0x809 for record in records[1:])):
        raise ValueError('legacy_spreadsheet_format_unsupported')
    return records, start + records[-1][0] + 4


def _walk(data, depth=0):
    if depth > 32:
        raise ValueError('spreadsheet_image_limit')
    for _, kind, options, content in _records(data, office=True):
        yield kind, options, content
        if options & 15 == 15:
            yield from _walk(content, depth + 1)


def _blips(group):
    records = list(_walk(group))
    if any(kind not in {0xf000, 0xf001, 0xf006, 0xf007, 0xf00b, 0xf11e} for kind, _, _ in records):
        raise ValueError('legacy_spreadsheet_drawing_unsupported')
    stores = [(options, content) for kind, options, content in records if kind == 0xf001]
    if len(stores) > 1:
        raise ValueError('legacy_spreadsheet_store_ambiguous')
    if not stores:
        return []
    options, content = stores[0]
    entries = list(_records(content, office=True))
    if len(entries) != options >> 4 or len(entries) > 1000:
        raise ValueError('legacy_spreadsheet_store_incomplete')
    images = []
    for _, kind, _, entry in entries:
        if kind != 0xf007 or len(entry) < 36:
            raise ValueError('legacy_spreadsheet_blip_unsupported')
        size, references = struct.unpack_from('<II', entry, 20)
        start = 36 + entry[33]
        if not references and len(entry) == start:
            images.append(None)
            continue
        embedded = list(_records(entry[start:], office=True))
        if len(embedded) != 1 or size != len(entry) - start:
            raise ValueError('legacy_spreadsheet_blip_incomplete')
        _, kind, options, content = embedded[0]
        formats = {
            0xf01d: ('.jpg', {0x46a: 17, 0x46b: 33, 0x6e2: 17, 0x6e3: 33}),
            0xf01e: ('.png', {0x6e0: 17, 0x6e1: 33}),
            0xf029: ('.tiff', {0x6e4: 17, 0x6e5: 33}),
            0xf02a: ('.jpg', {0x46a: 17, 0x46b: 33, 0x6e2: 17, 0x6e3: 33}),
        }
        if kind not in formats or options & 15:
            raise ValueError('legacy_spreadsheet_blip_unsupported')
        suffix, offsets = formats[kind]
        offset = offsets.get(options >> 4)
        if offset is None or len(content) <= offset:
            raise ValueError('legacy_spreadsheet_blip_incomplete')
        images.append((content[offset:], suffix, references))
    return images


def _sheet_images(drawing, blips, name):
    images = []
    for kind, _, content in _walk(drawing):
        if kind not in {0xf002, 0xf003, 0xf004, 0xf008, 0xf009, 0xf00a, 0xf00b, 0xf010, 0xf011}:
            raise ValueError('legacy_spreadsheet_shape_unsupported')
        if kind != 0xf004:
            continue
        properties, anchors, shapes, clients = [], [], [], []
        for _, kind, options, value in _records(content, office=True):
            if kind == 0xf00a:
                shapes.append((options >> 4, value))
            elif kind == 0xf00b:
                count = options >> 4
                if options & 15 != 3 or len(value) != count * 6:
                    raise ValueError('legacy_spreadsheet_property_incomplete')
                for index in range(count):
                    property_id, number = struct.unpack_from('<HI', value, index * 6)
                    if property_id & 0x8000 or property_id & 0x4000 and property_id != 0x4104:
                        raise ValueError('legacy_spreadsheet_property_unsupported')
                    if property_id & 0x3fff == 0x104:
                        if property_id != 0x4104:
                            raise ValueError('legacy_spreadsheet_blip_unsupported')
                        properties.append(number)
            elif kind == 0xf010:
                anchors.append(value)
            elif kind == 0xf011:
                clients.append(value)
        if len(shapes) != 1 or len(shapes[0][1]) != 8:
            raise ValueError('legacy_spreadsheet_shape_incomplete')
        shape_type, shape = shapes[0]
        flags = struct.unpack_from('<I', shape, 4)[0]
        if flags & 5 == 5 and not properties and not clients:
            continue
        if shape_type != 75 or flags & 0x12 or len(properties) != 1 or len(anchors) != 1 or len(clients) != 1:
            raise ValueError('legacy_spreadsheet_shape_unsupported')
        index = properties[0]
        if not 1 <= index <= len(blips) or blips[index - 1] is None or len(anchors[0]) != 18:
            raise ValueError('legacy_spreadsheet_image_unresolved')
        _, col, _, row, _, end_col, _, end_row, _ = struct.unpack('<9H', anchors[0])
        if col > 255 or end_col > 255 or end_col < col or end_row < row:
            raise ValueError('legacy_spreadsheet_anchor_invalid')
        image, suffix, _ = blips[index - 1]
        images.append((image, suffix, f'{name}!{get_column_letter(col + 1)}{row + 1}', index))
    return images


def images(path: Path):
    if path.stat().st_size > 300_000_000:
        raise ValueError('spreadsheet_image_limit')
    document = CompDoc(path.read_bytes(), logfile=StringIO())
    workbook = document.get_named_stream('Workbook')
    if workbook is None:
        raise ValueError('legacy_spreadsheet_format_unsupported')
    global_records, global_end = _substream(workbook, 0, 5)
    sheets, group = [], bytearray()
    continuation = False
    for _, kind, _, content in global_records:
        if kind in {0x2f, 0x7f, 0xe9, 0xec, 0x866}:
            raise ValueError('legacy_spreadsheet_image_unsupported')
        if kind == 0x85:
            if len(content) < 8 or content[5] != 0 or content[7] not in {0, 1}:
                raise ValueError('legacy_spreadsheet_sheet_unsupported')
            length = content[6] * (2 if content[7] else 1)
            if len(content) != 8 + length:
                raise ValueError('legacy_spreadsheet_sheet_incomplete')
            name = bytes(content[8:]).decode('utf-16le' if content[7] else 'latin1')
            sheets.append((struct.unpack_from('<I', content)[0], name))
        if kind == 0xeb or kind == 0x3c and continuation:
            group.extend(content)
            continuation = True
        else:
            continuation = False
    starts = [start for start, _ in sheets]
    if not sheets or len(sheets) > 1000 or len(set(starts)) != len(starts) or min(starts) != global_end:
        raise ValueError('legacy_spreadsheet_sheet_ownership_incomplete')
    sheet_records = {}
    ordered = sorted(starts)
    for index, start in enumerate(ordered):
        records, end = _substream(workbook, start, 0x10)
        if index + 1 < len(ordered):
            if end != ordered[index + 1]:
                raise ValueError('legacy_spreadsheet_sheet_ownership_incomplete')
        elif any(workbook[end:]):
            raise ValueError('legacy_spreadsheet_stream_incomplete')
        sheet_records[start] = records
    blips = _blips(group)
    output = []
    for start, name in sheets:
        drawing = bytearray()
        objects = 0
        continuation = False
        for _, kind, _, content in sheet_records[start]:
            if kind == 0xa:
                break
            if kind == 0xec or kind == 0x3c and continuation:
                drawing.extend(content)
                continuation = True
            else:
                continuation = False
            if kind in {0x7f, 0xe9, 0x866}:
                raise ValueError('legacy_spreadsheet_image_unsupported')
            if kind == 0x5d:
                parts = list(_records(content))
                if not parts or parts[0][1] != 0x15 or len(parts[0][3]) != 18 or struct.unpack_from('<H', parts[0][3])[0] != 8:
                    raise ValueError('legacy_spreadsheet_object_unsupported')
                if [part[1] for part in parts] != [0x15, 7, 8, 0]:
                    raise ValueError('legacy_spreadsheet_object_unsupported')
                if len(parts[1][3]) != 2 or len(parts[2][3]) != 2 or parts[3][3]:
                    raise ValueError('legacy_spreadsheet_object_incomplete')
                if struct.unpack('<H', parts[2][3])[0] & ~0x101:
                    raise ValueError('legacy_spreadsheet_object_unsupported')
                objects += 1
        extracted = _sheet_images(drawing, blips, name)
        if len(extracted) != objects:
            raise ValueError('legacy_spreadsheet_drawing_incomplete')
        output.extend(extracted)
        if len(output) > 1000:
            raise ValueError('spreadsheet_image_limit')
    references = Counter(index for _, _, _, index in output)
    if any(image is not None and references[index] != image[2] for index, image in enumerate(blips, 1)):
        raise ValueError('legacy_spreadsheet_image_ownership_incomplete')
    return [(content, suffix, cell) for content, suffix, cell, _ in output]
