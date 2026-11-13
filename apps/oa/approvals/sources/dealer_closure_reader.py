from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import datetime
import importlib.util
import json
import re
import sys


_MAX_SAP_ROWS = 1_000_000

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

    def values_condition(field, values):
        if not values or any(not re.fullmatch(r'[A-Za-z0-9]{1,12}', str(v)) for v in values):
            raise ValueError('dealer_closure_sap_scope_invalid')
        return field + ' IN (' + ','.join("'" + v + "'" for v in sorted(values)) + ')'

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
        out['credit_currencies'] = {r['KKBER']: read('T014', ['KKBER', 'WAERS'], [f"KKBER = '{r['KKBER']}'"])
                                    for r in q['credit']['rows']}
        customers = {kunnr}
        groups = {r['ZJXS'] for r in q['merge_group']['rows']}
        if groups:
            q['group_members'] = read('ZTCREDITLIMIT01', ['ZJXS', 'ZJXSBM'], [values_condition('ZJXS', groups)])
            customers.update(r['ZJXSBM'] for r in q['group_members']['rows'])
        customers.update(r['KUNN2'] for r in q['payer']['rows'] if r['KUNN2'])
        refs = {r['KNKLI'] or r['KUNNR'] for r in q['credit']['rows']}
        refs.update(customers)
        for ref in sorted(refs):
            scope = values_condition('KNKLI', {ref})
            q['orders-' + ref] = read('S066', ['KNKLI', 'KKBER', 'SPMON', 'OEIKW'], [scope])
            q['deliveries_invoices-' + ref] = read('S067', ['KNKLI', 'KKBER', 'SPTAG', 'OLIKW', 'OFAKW'], [scope])
        q['contracts'] = read('VAKPA', ['KUNDE', 'VKORG', 'PARVW', 'VBELN'],
                              [values_condition('KUNDE', customers), "AND PARVW = 'RG'"])
        out['contract_status'] = {r['VBELN']: read('VBUK', ['VBELN', 'RFGSK'], [f"VBELN = '{r['VBELN']}'"])
                                  for r in q['contracts']['rows']}
        out['contract_amounts'] = {}
        for document, status in out['contract_status'].items():
            if not any(r['RFGSK'] == 'A' for r in status['rows']):
                continue
            header = read('VBAK', ['VBELN', 'VKORG', 'VBTYP', 'WAERK', 'AUDAT'], [f"VBELN = '{document}'"])
            if not any(r['VBTYP'] == 'G' for r in header['rows']):
                continue
            items = read('VBAP', ['VBELN', 'POSNR', 'KZWI2', 'ABGRU'], [f"VBELN = '{document}' AND ABGRU = ''"])
            item_status = read('VBUP', ['VBELN', 'POSNR', 'RFSTA'], [f"VBELN = '{document}' AND RFSTA = 'A'"])
            eligible = {r['POSNR'] for r in item_status['rows']}
            following = []
            for item in items['rows']:
                if item['POSNR'] not in eligible:
                    continue
                links = read('VBFA', ['VBELV', 'POSNV', 'VBELN', 'POSNN', 'VBTYP_N'],
                    [f"VBELV = '{document}' AND POSNV = '{item['POSNR']}'", "AND VBTYP_N = 'C'"])
                amounts = [read('VBAP', ['VBELN', 'POSNR', 'KZWI2'],
                    [f"VBELN = '{r['VBELN']}' AND POSNR = '{r['POSNN']}'"]) for r in links['rows']]
                following.append({'item': item['POSNR'], 'links': links, 'amounts': amounts})
            out['contract_amounts'][document] = {'header': header, 'items': items,
                'item_status': item_status, 'following_orders': following}
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
            groups = defaultdict(set)
            for row in index['rows']:
                groups[row['GJAHR']].add(row['BELNR'])
            seen, target, batches = set(), [], []
            for year, docs in groups.items():
                docs = sorted(docs)
                for start in range(0, len(docs), 100):
                    batch = docs[start:start + 100]
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
        for program in ['ZSSR16', 'ZFRR04', 'ZFRR08']:
            response = connection.call('RPY_PROGRAM_READ', PROGRAM_NAME=program, ONLY_SOURCE='X', WITH_INCLUDELIST='X')
            out['programs'][program] = response
        return out
    finally:
        connection.close()


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
