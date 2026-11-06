from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from apps.oa.approvals.sources.http import urlopen


ENDPOINT = 'https://bi.synear.cn/decision/view/form'
SOURCE_URL = ENDPOINT + '?' + urlencode({'viewlet': 'other/开票信息.frm'})
WORKFLOWS = frozenset({
    'travel_expense', 'business_entertainment', 'business_support_fee',
    'small_marketing_fee', 'tpm_small_marketing_fee', 'general_expense',
})
MAX_RESPONSE_BYTES = 2_000_000


class InvoiceTitleReadError(RuntimeError):
    pass


class _Cells(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'td':
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.current is not None:
            self.cells.append(''.join(self.current))
            self.current = None


def snapshot():
    started_at = datetime.now(timezone.utc).isoformat()
    deadline = monotonic() + 60

    def read(request):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ValueError('deadline')
        with urlopen(request, timeout=min(15, remaining)) as response:
            if response.getcode() != 200:
                raise ValueError('response_status')
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError('response_size')
        return body.decode('utf-8')

    try:
        bootstrap = read(Request(SOURCE_URL))
        widget_urls = re.findall(r'"widgetUrl"\s*:\s*"([^"\r\n]+)"', bootstrap)
        session_ids = {
            session_id
            for url in widget_urls
            for session_id in parse_qs(urlsplit(url).query).get('sessionID', [])
        }
        if len(session_ids) != 1:
            raise ValueError('session_missing')
        session_id = session_ids.pop()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', session_id):
            raise ValueError('session_invalid')

        def report(widget, parameters):
            data = urlencode({
                'op': 'fr_form', 'cmd': 'load_report_content', 'widgetName': widget,
                '__parameters__': json.dumps(parameters, ensure_ascii=False),
                'noCache': 'lazy', 'pageIndex': '1', '__boxModel__': 'true',
                'reload': 'null', '_PAPERWIDTH': '1920', '_PAPERHEIGHT': '843',
                '_SHOWPARA': 'true', '_SHOWPARATEMPLATE': 'false',
            }).encode()
            result = json.loads(read(Request(ENDPOINT, data=data, headers={
                'sessionID': session_id, 'Content-Type': 'application/x-www-form-urlencoded',
            })))
            if (not isinstance(result, dict) or result.get('totalPage') != 1
                    or result.get('currentPage') != 1 or not isinstance(result.get('htmlTag'), str)):
                raise ValueError('report_shape')
            parser = _Cells()
            parser.feed(result['htmlTag'])
            return parser.cells

        names = [cell for cell in report('REPORT1', None)
                 if cell and cell != '开票人员开票时间节点要求']
        if not 1 <= len(names) <= 100 or len(set(names)) != len(names):
            raise ValueError('company_list')
        rows = []
        for name in names:
            encoded_name = ''.join(f'[{ord(char):04x}]' if ord(char) > 127 else char for char in name)
            cells = report('REPORT0', {'name': encoded_name, 'animateType': 'none'})
            fields = {}
            for index, cell in enumerate(cells[:-1]):
                label = cell.rstrip(':：')
                if label not in {'公司名称', '税号'}:
                    continue
                if label in fields:
                    raise ValueError('duplicate_field')
                fields[label] = cells[index + 1]
            if fields.get('公司名称') != name or not re.fullmatch(r'[A-Z0-9]{15,20}', fields.get('税号', '')):
                raise ValueError('company_fields')
            rows.append({'company_name': name, 'tax_id': fields['税号']})
        if len({row['tax_id'] for row in rows}) != len(rows):
            raise ValueError('duplicate_tax_id')
        return {
            'status': 'ready', 'source': 'feishu_workbench_invoice_info',
            'source_url': SOURCE_URL, 'observed_at': started_at,
            'completed_at': datetime.now(timezone.utc).isoformat(), 'rows': rows,
        }
    except Exception:
        raise InvoiceTitleReadError('invoice_title_reference_read_failed') from None
