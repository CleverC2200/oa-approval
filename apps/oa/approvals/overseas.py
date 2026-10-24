from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re

from apps.oa.approvals import forms as evidence_packet
from apps.oa.approvals import flows as flow

WORKFLOW_ID = '199521'

WORKFLOW_NAME = '海外采购付款流程（Application Process for Overseas Purchase Payment Request）'

RPA_NODE_ID = '223128'

CORE_FIELDS = ('fpje', 'fphm', 'fpdqr')

FIELDS = (*CORE_FIELDS, 'invoice_date', 'invoice_purchase_order')

LABELS = {'fpje': '发票金额', 'fphm': '发票号码', 'fpdqr': '发票到期日',
          'invoice_date': '发票日期', 'invoice_purchase_order': '发票采购订单号'}

def digest(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()

def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)

def _fields(record: dict) -> dict:
    items = record.get('workflowRequestTableFields')
    _require(isinstance(items, list) and bool(items), 'invoice_fields_invalid')
    result = {}
    for item in items:
        name = item.get('fieldName')
        _require(isinstance(name, str) and bool(name) and name not in result, 'invoice_fields_invalid')
        _require('fieldValue' in item, 'invoice_field_unreadable')
        result[name] = item
    return result

def _values(record: dict) -> dict:
    return {name: field['fieldValue'] for name, field in _fields(record).items()}


def _invoice_field_names(fields: dict) -> dict:
    names = {name: name for name in CORE_FIELDS}
    for name in ('invoice_date', 'invoice_purchase_order'):
        matches = [key for key, field in fields.items()
                   if flow.clean_field_text(field.get('fieldShowName')) == LABELS[name]]
        _require(len(matches) == 1, 'invoice_fields_missing_or_ambiguous')
        names[name] = matches[0]
    _require(len(set(names.values())) == len(FIELDS), 'invoice_fields_missing_or_ambiguous')
    return names


def snapshot(data: dict, resources: list[dict], *, writable: bool = True,
             empty_fields: dict | None = None) -> dict:
    identity = flow.workflow_identity(data)
    _require(identity == {'workflow_id': WORKFLOW_ID, 'workflow_name': WORKFLOW_NAME}, 'invoice_workflow_mismatch')
    _require(bool(str(data.get('requestId') or '')) and bool(str(data.get('currentNodeId') or '')), 'invoice_identity_invalid')
    if writable:
        _require(data.get('canEdit') is True, 'invoice_not_editable')
        _require(str(data['currentNodeId']) == RPA_NODE_ID, 'invoice_node_mismatch')
    main = data.get('workflowMainTableInfo', {}).get('requestRecords')
    _require(isinstance(main, list) and len(main) == 1, 'invoice_main_invalid')
    main_values = _values(main[0])
    currency = main_values.get('bz')
    _require(isinstance(currency, str) and re.fullmatch('[A-Z]{3}', currency) is not None, 'invoice_currency_missing')
    tables = data.get('workflowDetailTableInfos')
    _require(isinstance(tables, list) and bool(tables), 'invoice_details_missing')
    logical = evidence_packet.logical_attachment_resources(data, resources)
    declared = evidence_packet.extract_fields(data)
    rows, selected, seen_rows, seen_resources = [], [], set(), set()
    for ti, table in enumerate(tables):
        table_name = table.get('tableDBName')
        _require(isinstance(table_name, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', table_name) is not None, 'invoice_table_invalid')
        records = table.get('workflowRequestTableRecords')
        _require(isinstance(records, list), 'invoice_details_missing')
        for ri, record in enumerate(records):
            fields = _fields(record)
            order = record.get('recordOrder')
            _require(type(order) in (int, str) and re.fullmatch(r'\d+', str(order)) is not None and int(order) > 0, 'invoice_row_identity_missing')
            row_key = f'{table_name}:{order}'
            _require(row_key not in seen_rows, 'invoice_row_identity_conflict')
            seen_rows.add(row_key)
            if 'scfp' not in fields:
                rows.append({'key': row_key, 'table': ti, 'row': ri, 'values': _values(record), 'resource_ids': []})
                continue
            _require(set(CORE_FIELDS).issubset(fields), 'invoice_fields_missing')
            indexes = [i for i, f in enumerate(record['workflowRequestTableFields']) if f['fieldName'] == 'scfp']
            path = f'detail[{ti}].row[{ri}].field[{indexes[0]}]'
            bound = [r for r in logical if r.get('_trusted_source_field_path') == path]
            if not bound and empty_fields and path in empty_fields:
                _require(not fields['scfp'].get('fieldValue'), 'invoice_attachment_unbound')
                rows.append({'key': row_key, 'table': ti, 'row': ri, 'values': _values(record),
                             'resource_ids': [], 'invoice_empty_source': empty_fields[path]})
                continue
            _require(bool(bound), 'invoice_attachment_unbound')
            field_names = _invoice_field_names(fields)
            if writable:
                _require(all(fields[name].get('edit') is True for name in field_names.values()), 'invoice_field_not_editable')
            ids = []
            for resource in bound:
                rid = str(resource.get('id') or '')
                _require(rid.isdigit() and rid not in seen_resources, 'invoice_attachment_ambiguous')
                seen_resources.add(rid)
                ids.append(rid)
                selected.append(resource)
            raw_ids = [value.strip() for value in str(fields['scfp']['fieldValue'] or '').split(',')]
            _require((len(ids) == 1 or all(raw_ids)) and len(raw_ids) == len(set(raw_ids)) and len(ids) == len(raw_ids),
                     'invoice_attachment_unbound')
            if all(raw_ids):
                _require(set(ids) == set(raw_ids), 'invoice_attachment_unbound')
                ids = raw_ids
            orders = [field for field in declared if field['table'] == f'detail_{ti}'
                        and field['row'] == ri and field['name'] == '采购订单号']
            purchase_order = {'value': '', 'read_status': 'unresolved'}
            if len(orders) == 1:
                purchase_order = {key: orders[0][key] for key in ('value', 'read_status')}
                _require(orders[0]['field_name'] not in field_names.values(), 'invoice_purchase_order_field_conflict')
            rows.append({'key': row_key, 'table': ti, 'row': ri, 'values': _values(record),
                         'resource_ids': ids, 'field_names': field_names, 'purchase_order': purchase_order})
    _require(bool(selected) or any(row.get('invoice_empty_source') for row in rows),
             'invoice_attachments_missing')
    return {
        'request_id': str(data['requestId']), 'workflow_id': WORKFLOW_ID,
        'node_id': str(data['currentNodeId']), 'last_operate_time': data.get('lastOperateTime'),
        'currency': currency, 'main': main_values, 'rows': rows,
        'resources': [{k: r.get(k) for k in ('id', 'name', 'downloadUrl', 'createdate', 'createtime')} for r in selected],
    }

def stable_snapshot(snap: dict) -> dict:
    result = deepcopy(snap)
    for resource in result['resources']:
        resource.pop('downloadUrl', None)
    return result

def build_plan(material: dict, recognition: dict) -> dict:
    _require(set(recognition) == {'material_digest', 'invoices'} and recognition['material_digest'] == digest(material), 'invoice_recognition_material_mismatch')
    invoices = recognition['invoices']
    _require(isinstance(invoices, list), 'invoice_recognition_invalid')
    originals = {a['resource_id']: a for a in material['attachments']}
    if not originals:
        _require(not invoices, 'invoice_recognition_incomplete')
        _require(any(row.get('invoice_empty_source') for row in material['snapshot']['rows']),
                 'invoice_attachments_missing')
        return {'updates': [], 'remark': '未上传发票，保留原字段并提交当前 RPA 节点。'}
    by_id, updates, missing, errors = {}, [], [], []
    for invoice in invoices:
        _require(isinstance(invoice, dict) and set(invoice) == {'resource_id', 'sha256', 'currency', *FIELDS}, 'invoice_recognition_invalid')
        rid = invoice['resource_id']
        _require(rid in originals and invoice['sha256'] == originals[rid]['sha256'], 'invoice_recognition_attachment_mismatch')
        _require(invoice['currency'] == material['snapshot']['currency'], 'invoice_currency_mismatch')
        values = {}
        for field in FIELDS:
            item = invoice[field]
            _require(isinstance(item, dict) and set(item) == {'status', 'value', 'page', 'quote'}, 'invoice_recognition_invalid')
            _require(type(item['page']) is int and 1 <= item['page'] <= originals[rid]['pages'], 'invoice_page_invalid')
            _require(isinstance(item['quote'], str) and 0 < len(item['quote'].strip()) <= 500, 'invoice_evidence_missing')
            _require(item['status'] in {'recognized', 'missing'}, 'invoice_recognition_unresolved')
            if item['status'] == 'missing':
                _require(item['value'] is None, 'invoice_missing_value_invalid')
                continue
            value = item['value']
            _require(isinstance(value, str) and bool(value.strip()) and value == value.strip(), 'invoice_value_invalid')
            if field == 'fpje':
                _require(re.fullmatch(r'\d{1,30}(?:\.\d{1,2})?', value) is not None, 'invoice_amount_invalid')
                try:
                    amount = Decimal(value)
                    _require(amount.is_finite() and amount > 0, 'invoice_amount_invalid')
                    value = format(amount, '.2f')
                except InvalidOperation:
                    raise ValueError('invoice_amount_invalid') from None
            elif field in {'fpdqr', 'invoice_date'}:
                _require(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is not None, 'invoice_date_invalid')
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise ValueError('invoice_date_invalid') from None
            else:
                _require(len(value.encode('utf-8')) <= 200 and not any(ord(c) < 32 or c in '<>' for c in value), 'invoice_number_invalid')
            values[field] = value
        entries = by_id.setdefault(rid, {})
        number = values.get('fphm')
        _require(number not in entries, 'invoice_recognition_duplicate')
        page = invoice['fphm']['page'] if number else min(invoice[name]['page'] for name in FIELDS)
        entries[number] = {'values': values, 'page': page}
    _require(set(by_id) == set(originals), 'invoice_recognition_incomplete')
    _require(all(len(entries) == 1 or None not in entries for entries in by_id.values()),
             'invoice_multiple_invoices_need_identity')
    for row in material['snapshot']['rows']:
        if row['resource_ids']:
            sources = {}
            for rid in row['resource_ids']:
                content = originals[rid]['sha256']
                _require(content not in sources or sources[content] == by_id[rid], 'invoice_duplicate_conflict')
                sources.setdefault(content, by_id[rid])
            parts = [entry['values'] for entries in sources.values()
                     for entry in sorted(entries.values(), key=lambda item: item['page'])]
            numbers = [values['fphm'] for values in parts if 'fphm' in values]
            _require(len(numbers) == len(set(numbers)), 'invoice_number_conflict')
            if len(parts) == 1:
                values = {'fpje': parts[0]['fpje']} if 'fpje' in parts[0] else {}
            else:
                _require(all('fpje' in values for values in parts), 'invoice_aggregate_amount_missing')
                with localcontext() as context:
                    context.prec = 32 + len(str(len(parts)))
                    total = format(sum((Decimal(values['fpje']) for values in parts), Decimal('0')), '.2f')
                _require(len(total.split('.')[0]) <= 30, 'invoice_amount_invalid')
                values = {'fpje': total}
            for name in ('fphm', 'invoice_purchase_order'):
                joined = ','.join(part[name] for part in parts if name in part)
                if joined:
                    values[name] = joined
            for name in ('invoice_date', 'fpdqr'):
                if name in parts[0]:
                    values[name] = parts[0][name]
                else:
                    missing.append(f'第{row["row"] + 1}行首张发票未列明{LABELS[name]}')
            if any('fphm' not in part for part in parts):
                missing.append(f'第{row["row"] + 1}行部分发票未列明发票号码')
            if 'fpje' not in values:
                missing.append(f'第{row["row"] + 1}行发票金额未列明')
            selected = row['purchase_order']
            for index, part in enumerate(parts, 1):
                prefix = f'第{row["row"] + 1}行第{index}张发票'
                number = part.get('invoice_purchase_order')
                if not number:
                    errors.append(prefix + '采购订单号为空')
                elif selected['read_status'] != 'present' or not selected['value']:
                    errors.append(prefix + '对应的人工所选采购订单号未确认')
                elif number != selected['value'].strip():
                    errors.append(prefix + f'采购订单号{number}与所选采购订单号{selected["value"]}不一致')
            _require(bool(values), 'invoice_no_recognized_fields')
            updates.append({'row_key': row['key'], 'values': {row['field_names'][name]: value for name, value in values.items()}})
    remark = '已识别上传发票并回填可确认字段。'
    if errors:
        remark += '❌ ' + '；'.join(errors) + '。'
    if missing:
        remark += '；'.join(missing) + '，相应原值保留。'
    if any(row.get('invoice_empty_source') for row in material['snapshot']['rows']):
        remark += '无发票明细保留原字段。'
    return {'updates': updates, 'remark': remark}

def patched_workflow(data: dict, snap: dict, plan: dict) -> dict:
    result = deepcopy(data)
    by_key = {r['key']: r for r in snap['rows']}
    for update in plan['updates']:
        row = by_key[update['row_key']]
        record = result['workflowDetailTableInfos'][row['table']]['workflowRequestTableRecords'][row['row']]
        fields = _fields(record)
        for name, value in update['values'].items():
            _require(name in row['field_names'].values(), 'invoice_field_not_allowed')
            fields[name]['fieldValue'] = value
    return result

def _postcheck_comparison(after: dict, expected: dict) -> tuple[dict, dict, bool]:
    after, expected = stable_snapshot(after), stable_snapshot(expected)
    if expected['main'].get('lcbh') in (None, '') and 'lcbh' in expected['main']:
        value = after['main'].get('lcbh')
        if isinstance(value, str) and value.strip():
            after['main']['lcbh'] = expected['main']['lcbh']
    valid = True
    for rows in (after['rows'], expected['rows']):
        for row in rows:
            value = row['values'].get('fpje')
            if value not in (None, ''):
                try:
                    amount = Decimal(str(value))
                    if not amount.is_finite():
                        valid = False
                    else:
                        row['values']['fpje'] = amount
                except InvalidOperation:
                    valid = False
    return after, expected, valid

def postcheck_fields_match(after: dict, expected: dict) -> bool:
    after, expected, valid = _postcheck_comparison(after, expected)
    return valid and all(after[key] == expected[key] for key in
                         ('request_id', 'workflow_id', 'currency', 'main', 'resources', 'rows'))

def detail_data(data: dict, snap: dict, plan: dict) -> list[dict]:
    tables = {}
    by_key = {row['key']: row for row in snap['rows']}
    for update in plan['updates']:
        row = by_key[update['row_key']]
        table = data['workflowDetailTableInfos'][row['table']]
        record = table['workflowRequestTableRecords'][row['row']]
        table_name = table['tableDBName']
        target = tables.setdefault(table_name, {'tableDBName': table_name, 'workflowRequestTableRecords': []})
        target['workflowRequestTableRecords'].append({
            'recordOrder': record['recordOrder'],
            'workflowRequestTableFields': [{'fieldName': name, 'fieldValue': value}
                                          for name, value in update['values'].items()],
        })
    return list(tables.values())
