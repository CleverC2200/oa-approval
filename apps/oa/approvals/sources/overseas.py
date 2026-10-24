from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from apps.oa.approvals.sources import auth
from apps.oa.approvals.sources import http as safe_http
from apps.oa.approvals.sources import writer as writer_api
from apps.oa.approvals.sources.oa import OaApi
from apps.oa.approvals.sources.oa import TIMEOUT
from apps.oa.approvals.sources.oa import _decode_json
from apps.oa.approvals.overseas import CORE_FIELDS
from apps.oa.approvals.overseas import FIELDS
from apps.oa.approvals.overseas import _require


class InvoiceApi(OaApi):
    def submit_invoice(self, request_id: str, remark: str, details: list[dict], *, fields_by_row: dict) -> dict:
        _require(request_id.isdigit() and int(request_id) > 0, 'invoice_request_invalid')
        _require(isinstance(details, list), 'invoice_details_missing')
        seen = set()
        for table in details:
            _require(set(table) == {'tableDBName', 'workflowRequestTableRecords'}, 'invoice_payload_invalid')
            _require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', table['tableDBName']) is not None, 'invoice_table_invalid')
            _require(bool(table['workflowRequestTableRecords']), 'invoice_details_missing')
            for record in table['workflowRequestTableRecords']:
                _require(set(record) == {'recordOrder', 'workflowRequestTableFields'}, 'invoice_payload_invalid')
                order = str(record['recordOrder'])
                key = (table['tableDBName'], order)
                _require(order.isdigit() and int(order) > 0 and key not in seen, 'invoice_row_identity_invalid')
                seen.add(key)
                fields = record['workflowRequestTableFields']
                names = [field.get('fieldName') for field in fields]
                allowed = fields_by_row.get(key, set())
                _require(set(CORE_FIELDS) <= set(allowed) and len(allowed) == len(FIELDS), 'invoice_field_not_allowed')
                _require(bool(names) and len(set(names)) == len(names) and set(names) <= set(allowed), 'invoice_field_not_allowed')
                _require(all(set(field) == {'fieldName', 'fieldValue'} and isinstance(field['fieldValue'], str) for field in fields), 'invoice_payload_invalid')
        body = {
            'requestId': int(request_id), 'remark': writer_api._format_remark(remark),
            'otherParams': json.dumps({'src': 'submit', 'judgeFormMustInput': '1'}),
        }
        if details:
            body['detailData'] = json.dumps(details, ensure_ascii=False)
        request = urllib.request.Request(
            self.base_url + writer_api.SUBMIT_ENDPOINT,
            data=urllib.parse.urlencode(body).encode('utf-8'),
            headers={**self._headers(), 'content-type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        with safe_http.urlopen(request, timeout=TIMEOUT) as response:
            return _decode_json(response.read().decode('utf-8'))


def build_api() -> InvoiceApi:
    return InvoiceApi(*auth.credentials())
