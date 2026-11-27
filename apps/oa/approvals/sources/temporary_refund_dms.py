from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys

from apps.oa.approvals.sources import dealer_closure
from apps.oa.approvals.sources.dealer_closure_reader import connect_read_source
from apps.oa.runtime.paths import PROJECT_ROOT


KA_CONTRACTS = """
    SELECT d.contract_code, d.customer_code, d.contract_subject,
           h.customer_code AS header_customer, h.contract_subject AS header_company,
           h.apply_type, h.contract_category, h.contract_status, h.delete_status,
           h.start_time, h.end_time, h.fiscal_year
    FROM `sn-dms`.dms_contract_delivery d
    LEFT JOIN `sn-dms`.dms_contract_header h ON h.contract_code=d.contract_code
    WHERE d.customer_code IN (%s, %s) AND (%s IS NULL OR d.contract_subject=%s)
    ORDER BY d.contract_code LIMIT 101
"""

REFUND_APPLICATIONS = """
    SELECT r.contract_code, r.customer_code, r.contract_subject, r.contract_subject_name, r.customer_name,
           h.customer_code AS header_customer, h.contract_subject AS header_company,
           r.create_time, r.refund_amount, r.refund_amount_upper_case, r.customer_sign_date,
           r.bank_account_name, r.bank_account_number, h.legal_person, h.contract_status, h.delete_status
    FROM `sn-dms`.dms_contract_refund_apply r
    LEFT JOIN `sn-dms`.dms_contract_header h ON h.contract_code=r.contract_code
    WHERE r.customer_code IN (%s, %s) AND (%s IS NULL OR r.contract_subject=%s)
    ORDER BY r.create_time DESC, r.contract_code DESC LIMIT 101
"""

MONTHLY_STATEMENTS = """
    SELECT contract_code, customer_code, contract_subject, customer_name,
           customer_code AS header_customer, contract_subject AS header_company,
           year_and_month, contract_status, delete_status
    FROM `sn-dms`.dms_contract_bill_header
    WHERE customer_code IN (%s, %s) AND (%s IS NULL OR contract_subject=%s OR contract_subject IS NULL)
      AND year_and_month=%s
    ORDER BY contract_code LIMIT 101
"""


def _query(cursor, sql, customer, company, *, statement_month=None):
    params = (customer, customer.zfill(10), company, company)
    if statement_month is not None:
        params += (statement_month,)
    cursor.execute(sql, params)
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    if len(rows) > 100:
        raise ValueError('temporary_refund_dms_result_truncated')
    if (len({row['contract_code'] for row in rows}) != len(rows)
            or any(not row['contract_code'] or row['contract_status'] is None
                   or str(row['customer_code']).lstrip('0') != customer
                   or str(row['header_customer']).lstrip('0') != customer
                   or row['contract_subject'] != row['header_company']
                   or (statement_month is None and not row['contract_subject'])
                   or (statement_month is not None and row['year_and_month'] != statement_month)
                   or (company is not None and row['contract_subject'] not in (company, None))
                   for row in rows)):
        raise ValueError('temporary_refund_dms_header_identity_invalid')
    return [{**row, 'dms_signature_completed': str(row['contract_status']) == '4'
             and str(row['delete_status']) == '1'} for row in rows]


def _account_text(value):
    return ''.join(str(value or '').split())


def _reported_accounts(customer, settings):
    try:
        result = dealer_closure._dms(customer, 'queryPaymentAccountReportByCustomerCode', settings)['response']
        reports = result['result']
    except (RuntimeError, KeyError):
        return None
    records = reports if isinstance(reports, list) else [reports] if reports is not None else []
    accounts = {}
    for report in records:
        subject = str(report.get('contractSubject') or '')
        for name_key, number_key in (('bankAccountName', 'bankAccountNumber'),
                                     ('bankAccountNameTmp', 'bankAccountNumberTmp')):
            name = _account_text(report.get(name_key))
            number = _account_text(report.get(number_key))
            if subject and name and number:
                accounts.setdefault(subject, []).append([name, number])
    return accounts


def _check_reported_accounts(documents, reports):
    for document in documents:
        name = _account_text(document.pop('bank_account_name'))
        number = _account_text(document.pop('bank_account_number'))
        legal_person = _account_text(document.pop('legal_person'))
        accounts = reports.get(document['contract_subject'], []) if reports is not None else []
        document['account'] = {
            'present': bool(name and number),
            'holder_matches_customer_name': bool(name and name == _account_text(document.get('customer_name'))),
            'holder_matches_dms_legal_person': name == legal_person if name and legal_person else None,
            'matched_company_report': [name, number] in accounts if accounts else None,
            'company_report_status': 'failed' if reports is None else 'read' if accounts else 'missing',
        }


def _collect(connector, company, customers, statement_month, account_reports):
    with closing(connect_read_source(connector)) as connection:
        try:
            results = {}
            with connection.cursor() as cursor:
                cursor.execute('START TRANSACTION READ ONLY')
                for customer in customers:
                    documents = _query(cursor, REFUND_APPLICATIONS, customer, company)
                    _check_reported_accounts(documents, account_reports[customer])
                    results[customer] = {
                        'ka_contracts': _query(cursor, KA_CONTRACTS, customer, company),
                        'refund_applications': documents,
                        'statements': _query(cursor, MONTHLY_STATEMENTS, customer, company,
                                             statement_month=statement_month),
                    }
            return results
        finally:
            connection.rollback()


def read(connector: Path, company, customers, statement_month):
    if (not connector.is_absolute() or connector.suffix != '.py' or connector.is_symlink()
            or not connector.is_file() or PROJECT_ROOT.resolve() in connector.resolve().parents):
        raise ValueError('temporary_refund_dms_connector_invalid')
    metadata = connector.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError('temporary_refund_dms_connector_not_private')
    config = dealer_closure.settings()
    try:
        completed = subprocess.run(
            [config['doris_python'], '-B', '-m', __name__],
            input=json.dumps({'connector': str(connector), 'company': company, 'customers': customers,
                              'statement_month': statement_month,
                              'account_reports': {customer: _reported_accounts(customer, config)
                                                  for customer in customers}}),
            cwd=PROJECT_ROOT, env={k: v for k, v in os.environ.items() if not k.startswith('OA_')},
            text=True, capture_output=True, timeout=60,
        )
        response = json.loads(completed.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise RuntimeError('temporary_refund_dms_read_failed') from None
    if completed.returncode:
        raise RuntimeError(response['error'])
    return response


if __name__ == '__main__':
    try:
        print(json.dumps(_collect(**json.load(sys.stdin)), ensure_ascii=False, default=str))
    except Exception as error:
        safe_errors = {'temporary_refund_dms_header_identity_invalid', 'temporary_refund_dms_result_truncated'}
        print(json.dumps({'error': str(error) if str(error) in safe_errors else 'temporary_refund_dms_read_failed'}))
        sys.exit(1)
