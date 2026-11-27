from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from zoneinfo import ZoneInfo

from apps.oa.approvals.sources import dealer_closure, dealer_finance_delivery, official_sql, temporary_refund_dms
from apps.oa.runtime.environment import stable_env_file


def _fields(row):
    return {field['fieldName']: field.get('fieldValue') for field in row['workflowRequestTableFields']}


def _customer(value):
    customer = str(value or '').strip().lstrip('0')
    if not re.fullmatch(r'[0-9]{1,10}', customer):
        raise ValueError('dealer_finance_customer_unverified')
    return customer


def _settings():
    path = stable_env_file().with_name('dealer-finance.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('dealer_finance_configuration_unavailable')
    settings = json.loads(path.read_text())
    groups = [[_customer(code) for code in group] for group in settings['adjustment_groups']]
    codes = [code for group in groups for code in group]
    if len(groups) != 2 or any(len(group) < 2 for group in groups) or len(codes) != len(set(codes)):
        raise ValueError('dealer_finance_adjustment_groups_invalid')
    return {**settings, 'adjustment_groups': groups}


def _read(source, call):
    try:
        return {'status': 'read', 'value': call()}
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        reason = str(error)
        return {'status': 'failed', 'reason': reason if re.fullmatch(r'[a-z0-9_]{1,90}', reason) else source + '_read_failed'}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def signed_adjustment_files(evidence, directory):
    file_origin = urlsplit(_settings().get('adjustment_file_origin', ''))
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    files = []
    for customer, sources in evidence['customers'].items():
        query = sources.get('dms', {}).get('queryLatestAdjustmentByCustomerCode', {})
        record = query.get('value')
        records = record if isinstance(record, list) else [record] if isinstance(record, dict) else []
        for result in records:
            if str(result.get('contractStatus')) != '4' or str(result.get('deleteStatus')) != '1':
                continue
            for attachment in result.get('contractAttachVoList') or ():
                if not attachment.get('signedFileKey'):
                    continue
                resource_id = f'dms-adjustment:{customer}:{attachment.get("id")}'
                if not re.fullmatch(r'dms-adjustment:[0-9]+:[0-9]+', resource_id):
                    raise ValueError('dealer_finance_dms_attachment_identity_invalid')
                parsed = urlsplit(attachment['fileUrl'])
                if (parsed.scheme not in {'http', 'https'} or parsed.scheme != file_origin.scheme
                        or parsed.netloc != file_origin.netloc or parsed.username or parsed.password
                        or parsed.path != '/file/v1/getFileStream'):
                    raise ValueError('dealer_finance_dms_attachment_origin_invalid')
                url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                  urlencode({'fileKey': attachment['signedFileKey']}), ''))
                try:
                    with opener.open(url, timeout=30) as response:
                        limit = 32 * 1024 * 1024
                        body = response.read(limit + 1)
                except OSError:
                    raise ValueError('dealer_finance_dms_attachment_read_failed') from None
                if not body.startswith(b'%PDF-') or len(body) > limit:
                    raise ValueError('dealer_finance_dms_attachment_invalid')
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / (resource_id.replace(':', '-') + '.pdf')
                with target.open('xb') as output:
                    output.write(body)
                files.append({'id': resource_id, 'name': 'DMS 调账申请书签署件.pdf',
                              'source': 'dms_contract', 'path': str(target)})
                attachment['review_resource_id'] = resource_id
    return files


def incomplete_reads(evidence):
    diagnostics = []
    for sources in evidence['customers'].values():
        reads = {key: outcome for key, outcome in sources.items() if key != 'company'}
        dms = reads.get('dms', {})
        if dms and 'status' not in dms:
            reads.pop('dms')
            reads.update(dms)
        diagnostics.extend({'stage': 'dealer_finance', 'source_path': source, 'reason': outcome['reason']}
                           for source, outcome in reads.items() if outcome['status'] == 'failed')
    return diagnostics


def collect(api, workflow, resources, *, origins):
    context = dealer_finance_delivery.snapshot(api, workflow, resources, origins=origins)
    configuration = _settings()
    connection = dealer_closure.settings()
    now = datetime.now(ZoneInfo('Asia/Taipei'))
    main = _fields(workflow['workflowMainTableInfo']['requestRecords'][0])
    details = [_fields(row) for table in workflow['workflowDetailTableInfos']
               for row in table['workflowRequestTableRecords']]
    refund = context['workflow_id'] == '195056'
    company = str(main.get('gsdm') or (main.get('bxgsdm') if refund else '') or '').strip() or None
    if company and not re.fullmatch(r'[A-Z0-9]{4}', company):
        raise ValueError('dealer_finance_company_invalid')
    items = []
    for row in details:
        if refund:
            items.append({'customer': _customer(row['jxskhbm']), 'amount': row['tkje']})
        else:
            outgoing = _customer(row.get('dcfjxsbm') or row.get('dckhbm'))
            incoming = _customer(row.get('drfjxsbm') or row.get('drkhbm'))
            items.append({'customer': outgoing, 'incoming_customer': incoming,
                          'amount': row.get('je') or row.get('dzje'),
                          'archived_remaining_order_limit': row.get('xdsyed'),
                          'fixed_group': any({outgoing, incoming} <= set(group)
                                             for group in configuration['adjustment_groups'])})
    if not items:
        raise ValueError('dealer_finance_details_empty')

    customers = list(dict.fromkeys(item['customer'] for item in items))
    evidence = {}
    for customer in customers:
        evidence[customer] = {'credit': _read('credit', lambda: dealer_closure._worker(
            'sap_credit', {'customer_code': customer}, connection)['credit_limit'])}
    if refund:
        statement_month = (now.date().replace(day=1) - timedelta(days=1)).strftime('%Y%m')
        refund_sources = _read('refund_dms', lambda: temporary_refund_dms.read(
            Path(configuration['refund_dms_connector']), company, customers, statement_month))
        for customer, result in evidence.items():
            result['dms'] = ({'status': 'read', 'value': refund_sources['value'][customer]}
                             if refund_sources['status'] == 'read' else refund_sources)
    else:
        account_tag = 'queryPaymentAccountReportByCustomerCode'
        statement_tag = 'queryLatestSignedBillByCustomerCode'
        adjustment_tag = 'queryLatestAdjustmentByCustomerCode'
        queries = {(item['customer'], tag) for item in items if not item['fixed_group']
                   for tag in (account_tag, statement_tag, adjustment_tag)}
        queries.update((item['incoming_customer'], tag) for item in items if not item['fixed_group']
                       for tag in (account_tag, statement_tag))
        for customer, tag in sorted(queries):
            evidence.setdefault(customer, {}).setdefault('dms', {})[tag] = _read(tag, lambda: dealer_closure._dms(
                customer, tag, connection)['response']['result'])

    for customer in customers:
        scope = company
        if not refund:
            configured_scope = configuration.get('adjustment_company_by_request', {}).get(
                context['request_id'], {}).get(customer)
            if configured_scope and not re.fullmatch(r'[A-Z0-9]{4}', configured_scope):
                raise ValueError('dealer_finance_company_invalid')
            scope = configured_scope or scope
        evidence[customer]['company'] = scope
        evidence[customer]['bi'] = (_read('cust_balance', lambda: dealer_closure._worker('doris', {
            'company_code': scope, 'customer_code': customer,
        }, connection)['rows']) if scope else {'status': 'company_pending'})
    history = []
    if str(context['actual_node']['currentnodetype']) == '3':
        history = official_sql._query_rows('dealer_finance_history', {
            'request_id': int(context['request_id']), 'workflow_id': int(context['workflow_id']),
        }, require_connection=True)
        if len(history) >= 1001 or any(len(str(row['remark'] or '')) != (row['remark_length'] or 0)
                                      for row in history):
            raise ValueError('dealer_finance_history_incomplete')
    return {'submission_context': context, 'flow_number': main.get('lcbh'), 'company_code': company,
            'observed_at': now.isoformat(), 'items': items, 'customers': evidence,
            'archived_history': history,
            **({'statement_month': statement_month} if refund else {}),
            'resource_inventory': {'count': len(resources), 'source': 'getRequestResources'}}
