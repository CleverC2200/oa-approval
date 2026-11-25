from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import importlib.util
import json
import re
import sys


_MAX_SAP_ROWS = 1_000_000
_PREACCRUAL_DOCUMENTS_PER_REQUEST = 400
_CREDIT_CONTROL_AREA = 'SN01'

def doris(payload):
    spec = importlib.util.spec_from_file_location('dealer_balance_connector', payload['connection']['doris_connector'])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = module.connect()
    sql = 'SELECT * FROM hive.dw.dwd_fin_cust_balance WHERE company_code=%s AND cust_code=%s'
    params = [payload['company_code'], payload['customer_code']]
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            names = [r[0] for r in cursor.description]
            values = cursor.fetchall()
            rows = values if not values or isinstance(values[0], dict) else [dict(zip(names, r)) for r in values]
        return {'status': 'ok', 'sql': sql, 'params': params, 'rows': rows}
    finally:
        connection.close()


def sap(payload):
    from pyrfc import Connection

    config = payload['connection']
    company, customer, cutoff = payload['company_code'], payload['customer_code'], payload['as_of']
    if not re.fullmatch(r'[A-Za-z0-9]{4}', company) or not re.fullmatch(r'[0-9]{1,10}', customer):
        raise ValueError('dealer_closure_identity_invalid')
    date = datetime.strptime(cutoff, '%Y%m%d')
    month = date.month - 6
    year = date.year
    if month <= 0:
        month += 12
        year -= 1
    begin = date.replace(year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1])).strftime('%Y%m%d')
    kunnr = customer.zfill(10)
    connection = Connection(ashost=config['sap_host'], sysnr=config['sap_sysnr'],
        client=config['sap_client'], user=config['sap_user'], passwd=payload['sap_password'], lang='ZH', trace='0')

    def read(table, fields, options):
        request = {'QUERY_TABLE': table, 'DELIMITER': '|', 'ROWCOUNT': 10000, 'GET_SORTED': 'X',
                   'FIELDS': [{'FIELDNAME': f} for f in fields], 'OPTIONS': [{'TEXT': s} for s in options]}

        def scan():
            values, pages = [], []
            first = None
            previous = None
            while True:
                page_request = {**request, 'ROWSKIPS': len(values)}
                response = connection.call('RFC_READ_TABLE', **page_request)
                page = response['DATA']
                if (len(page) > request['ROWCOUNT'] or len(values) + len(page) > _MAX_SAP_ROWS
                        or (page and page == previous)):
                    raise RuntimeError('dealer_closure_sap_rows_truncated')
                if first is None:
                    first = response
                pages.append({'request': page_request, 'row_count': len(page)})
                values.extend(page)
                if len(page) < request['ROWCOUNT']:
                    return {**first, 'DATA': values}, pages
                previous = page

        try:
            response, pages = scan()
            verification = []
            if len(pages) > 1:
                repeated, verification = scan()
                if response['DATA'] != repeated['DATA']:
                    raise RuntimeError('dealer_closure_sap_snapshot_changed')
            rows = []
            for item in response['DATA']:
                parts = item['WA'].split('|', len(fields) - 1)
                if len(parts) != len(fields):
                    raise RuntimeError('dealer_closure_sap_columns_invalid')
                rows.append(dict(zip(fields, [p.strip() for p in parts])))
            return {'request': request, 'response': response, 'rows': rows, 'complete': True,
                    'pages': pages, 'verification_pages': verification}
        except Exception as error:
            error.source_stage = table
            raise

    try:
        info = connection.call('RFC_SYSTEM_INFO')['RFCSI_EXPORT']
        if info['RFCSYSID'] != config['sap_system'] or info['RFCIPADDR'] != config['sap_host']:
            raise RuntimeError('dealer_closure_sap_environment_mismatch')
        out = {'status': 'ok', 'company_code': company, 'customer_code': customer,
               'identity': {k: info[k] for k in ['RFCSYSID', 'RFCIPADDR', 'RFCHOST2']},
               'cutoff': cutoff, 'financing_window_start': begin, 'queries': {}, 'programs': {}}
        q = out['queries']
        q['client'] = read('T000', ['MANDT', 'MTEXT', 'CCCATEGORY', 'LOGSYS'], ["MANDT = '" + config['sap_client'] + "'"])
        if len(q['client']['rows']) != 1 or q['client']['rows'][0]['CCCATEGORY'] != 'P':
            raise RuntimeError('dealer_closure_sap_client_not_production')
        q['company'] = read('T001', ['BUKRS', 'BUTXT', 'WAERS'], [f"BUKRS = '{company}'"])
        if len(q['company']['rows']) != 1:
            raise RuntimeError('dealer_closure_sap_company_unavailable')
        for label, table, fields, options in [
            ('customer_master', 'KNA1', ['KUNNR', 'NAME1', 'NAME2', 'LIFSD'], [f"KUNNR = '{kunnr}'"]),
            ('credit', 'KNKK', ['KUNNR', 'KKBER', 'KNKLI', 'KLIMK', 'SKFOR', 'SSOBL', 'CRBLB'], [f"KUNNR = '{kunnr}'"]),
            ('payer', 'KNVP', ['KUNNR', 'VKORG', 'VTWEG', 'SPART', 'PARVW', 'KUNN2'], [f"KUNNR = '{kunnr}' AND PARVW = 'RG'"]),
            ('sales', 'KNVV', ['KUNNR', 'VKORG', 'VTWEG', 'SPART', 'AUFSD', 'LIFSD'], [f"KUNNR = '{kunnr}'"]),
            ('company_accounts', 'KNB1', ['KUNNR', 'BUKRS', 'AKONT', 'SPERR'], [f"KUNNR = '{kunnr}' AND BUKRS = '{company}'"]),
            ('merge_group', 'ZTCREDITLIMIT01', ['ZJXS', 'ZJXSBM'], [f"ZJXSBM = '{kunnr}'"]),
        ]:
            q[label] = read(table, fields, options)
        credit_areas = {row['KKBER'] for row in q['credit']['rows']} | {_CREDIT_CONTROL_AREA}
        out['credit_currencies'] = {area: read('T014', ['KKBER', 'WAERS'], [f"KKBER = '{area}'"])
                                    for area in sorted(credit_areas)}
        credit_groups = {row['ZJXS'] for row in q['merge_group']['rows']}
        if credit_groups:
            if len(credit_groups) != 1 or not re.fullmatch(r'[A-Za-z0-9]{1,12}', next(iter(credit_groups))):
                raise RuntimeError('dealer_closure_credit_group_ambiguous')
            group, = credit_groups
            q['group_members'] = read('ZTCREDITLIMIT01', ['ZJXS', 'ZJXSBM'], [f"ZJXS = '{group}'"])
            if not any(row['ZJXS'] == group and row['ZJXSBM'] == kunnr for row in q['group_members']['rows']):
                raise RuntimeError('dealer_closure_credit_group_unverified')
        fields = ['BUKRS', 'KUNNR', 'BELNR', 'GJAHR', 'BUZEI', 'BUDAT', 'BLART', 'HKONT', 'UMSKZ', 'SHKZG', 'DMBTR', 'XNEGP', 'SGTXT']
        q['open_items'] = read('BSID', fields, [f"KUNNR = '{kunnr}' AND BUKRS = '{company}'"])
        q['cleared_period'] = read('BSAD', fields, [f"KUNNR = '{kunnr}' AND BUKRS = '{company}'", f"AND BUDAT >= '{begin}' AND BUDAT <= '{cutoff}'"])
        q['actual_deliveries'] = read('LIKP', ['VBELN', 'KUNAG', 'KUNNR', 'WADAT_IST', 'LFDAT'],
                                     [f"KUNAG = '{kunnr}'", f"AND WADAT_IST >= '20240101' AND WADAT_IST <= '{cutoff}'"])
        out['headers'] = {}
        for row in q['open_items']['rows'] + q['cleared_period']['rows']:
            key = '-'.join(row[f] for f in ['BUKRS', 'BELNR', 'GJAHR'])
            if key not in out['headers']:
                out['headers'][key] = read('BKPF', ['BUKRS', 'BELNR', 'GJAHR', 'BLART', 'BUDAT', 'BKTXT', 'STBLG'],
                    [f"BUKRS = '{row['BUKRS']}' AND BELNR = '{row['BELNR']}'", f"AND GJAHR = '{row['GJAHR']}'"])
        out['preaccrual'] = []
        for account in payload.get('preaccrual_accounts', []):
            if not re.fullmatch(r'[0-9]{10}', account):
                raise ValueError('dealer_closure_preaccrual_account_invalid')
            index = read('BSIS', ['BUKRS', 'HKONT', 'GJAHR', 'BELNR', 'BUZEI', 'BUDAT', 'SHKZG', 'DMBTR', 'KOSTL', 'XNEGP'],
                [f"BUKRS = '{company}' AND HKONT = '{account}'", f"AND BUDAT <= '{cutoff}'"])
            indexed = {(r['BELNR'], r['GJAHR'], r['BUZEI']): r for r in index['rows']}
            documents_by_fiscal_year = defaultdict(set)
            for row in index['rows']:
                documents_by_fiscal_year[row['GJAHR']].add(row['BELNR'])
            seen, target, batches = set(), [], []
            for year, docs in documents_by_fiscal_year.items():
                docs = sorted(docs)
                for start in range(0, len(docs), _PREACCRUAL_DOCUMENTS_PER_REQUEST):
                    batch = docs[start:start + _PREACCRUAL_DOCUMENTS_PER_REQUEST]
                    options = [f"BUKRS = '{company}' AND GJAHR = '{year}'", f"AND HKONT = '{account}'", 'AND BELNR IN (']
                    for n in range(0, len(batch), 4):
                        options.append(','.join("'" + v + "'" for v in batch[n:n + 4]) + (',' if n + 4 < len(batch) else ''))
                    options.append(')')
                    result = read('BSEG', ['BUKRS', 'BELNR', 'GJAHR', 'BUZEI', 'HKONT', 'XREF1'], options)
                    for row in result['rows']:
                        key = (row['BELNR'], row['GJAHR'], row['BUZEI'])
                        if key in indexed:
                            seen.add(key)
                            if ''.join(row['XREF1'].split()).zfill(10) == kunnr:
                                target.append({**indexed[key], 'XREF1': row['XREF1']})
                    batches.append({'options': options, 'rows': len(result['rows'])})
            if len(seen) != len(indexed):
                raise RuntimeError('dealer_closure_preaccrual_coverage_incomplete')
            details = []
            for row in target:
                key = '-'.join(row[f] for f in ['BUKRS', 'BELNR', 'GJAHR'])
                if key not in out['headers']:
                    out['headers'][key] = read('BKPF', ['BUKRS', 'BELNR', 'GJAHR', 'BLART', 'BUDAT', 'BKTXT', 'STBLG'],
                        [f"BUKRS = '{row['BUKRS']}' AND BELNR = '{row['BELNR']}'", f"AND GJAHR = '{row['GJAHR']}'"])
                details.append(read('BSEG', ['BUKRS', 'BELNR', 'GJAHR', 'BUZEI', 'HKONT', 'SHKZG', 'DMBTR', 'XNEGP', 'XREF1', 'SGTXT'],
                    [f"BUKRS = '{row['BUKRS']}' AND BELNR = '{row['BELNR']}'",
                     f"AND GJAHR = '{row['GJAHR']}' AND BUZEI = '{row['BUZEI']}'"]))
            out['preaccrual'].append({'account': account, 'index_rows': len(indexed),
                'index_keys_seen': len(seen), 'complete': True, 'target_rows': target,
                'index_request': index['request'], 'index_pages': index['pages'],
                'index_verification_pages': index['verification_pages'], 'reference_batches': batches, 'target_details': details})
        for program in ['ZFRR04', 'ZFRR08']:
            response = connection.call('RPY_PROGRAM_READ', PROGRAM_NAME=program, ONLY_SOURCE='X', WITH_INCLUDELIST='X')
            out['programs'][program] = response
        out['credit_limit'] = credit_limit(connection, kunnr, q['payer']['rows'], bool(credit_groups))
        return out
    finally:
        connection.close()


def credit_limit(connection, customer, payer_rows, merged_credit):
    function = 'ZSD_CRM_CREDITLIMIT_EXPORT'
    try:
        response = connection.call(function, KUNNR=customer)
        messages = response.get('RETURN') if isinstance(response, dict) else None
        if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
            raise RuntimeError('dealer_closure_credit_response_invalid')
        if any(message.get('TYPE') in ('E', 'A', 'X') for message in messages):
            raise RuntimeError('dealer_closure_credit_business_error')
        resolves_payer = customer[2:4] != '49'
        payers = {row['KUNN2'].zfill(10) for row in payer_rows if re.fullmatch(r'[0-9]{1,10}', row['KUNN2'])}
        scoped_ids = {customer}
        if resolves_payer and not merged_credit:
            scoped_ids.update(payers)
        success_ids = [str(message.get('ID', '')).strip() for message in messages if message.get('TYPE') == 'S']
        scoped_success = bool(success_ids) and all(
            re.fullmatch(r'[0-9]{1,10}', identity) and identity.zfill(10) in scoped_ids for identity in success_ids)
        group_filtered_success = (not messages and merged_credit and resolves_payer
                                  and len(payers) == 1 and customer not in payers)
        if not (scoped_success or group_filtered_success):
            raise RuntimeError('dealer_closure_credit_identity_unverified')

        occupancy_fields = ('O_ORDER', 'O_DELIVERY', 'O_INVOICE', 'O_CONTRACT')
        amounts = {}
        for field in ('O_DKLIMK', *occupancy_fields):
            raw = str(response.get(field)).strip()
            try:
                amount = Decimal('-' + raw[:-1] if raw.endswith('-') else raw)
            except InvalidOperation:
                raise RuntimeError('dealer_closure_credit_response_invalid') from None
            if not amount.is_finite():
                raise RuntimeError('dealer_closure_credit_response_invalid')
            amounts[field] = amount
        return {'source': function, 'customer_code': customer, 'credit_control_area': _CREDIT_CONTROL_AREA,
                'remaining_order_limit': str(amounts['O_DKLIMK']), 'order_occupancy': str(sum(amounts[field] for field in occupancy_fields)), 'response': response}
    except Exception as error:
        error.source_stage = 'credit_limit'
        raise


def failure(error):
    reason = str(error)
    if not re.fullmatch(r'dealer_closure_[a-z_]{1,45}', reason):
        reason = 'dealer_closure_source_failed'
    return {'status': 'failed', 'error_type': type(error).__name__, 'reason': reason,
            'stage': getattr(error, 'source_stage', 'source'),
            'error_key': getattr(error, 'key', '')}


def main():
    try:
        payload = json.load(sys.stdin)
        result = {'doris': doris, 'sap': sap}[sys.argv[1]](payload)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as error:
        print(json.dumps(failure(error)))
        return 1


if __name__ == '__main__':
    sys.exit(main())
