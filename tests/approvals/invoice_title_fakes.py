from html import escape
from io import BytesIO
import json
from urllib.parse import parse_qs

from apps.oa.approvals.sources import invoice_titles


ROWS = [
    {'company_name': '示例（杭州）食品有限公司', 'tax_id': 'A' * 18},
    {'company_name': '另一(杭州)测试有限公司 ', 'tax_id': 'B' * 18},
    {'company_name': '示例企业（有限合伙）', 'tax_id': 'C' * 18},
]


class Response(BytesIO):
    def getcode(self):
        return 200


class InvoiceTitleTransport:
    def __init__(self):
        self.requests = []
        self.responses = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        assert 0 < timeout <= 15
        if request.data is None:
            assert request.full_url == invoice_titles.SOURCE_URL
            body = 'var config = {"widgetUrl":"/decision/view/form?sessionID=private_session"};'
        else:
            assert request.full_url == invoice_titles.ENDPOINT
            assert request.get_header('Sessionid') == 'private_session'
            parameters = parse_qs(request.data.decode())
            assert parameters['op'] == ['fr_form']
            assert parameters['cmd'] == ['load_report_content']
            if parameters['widgetName'] == ['REPORT1']:
                values = [row['company_name'] for row in ROWS] + ['开票人员开票时间节点要求']
            else:
                assert parameters['widgetName'] == ['REPORT0']
                selected = json.loads(parameters['__parameters__'][0])['name']
                row = next(row for row in ROWS if selected == ''.join(
                    f'[{ord(char):04x}]' if ord(char) > 127 else char for char in row['company_name']))
                values = ['', '公司名称:', row['company_name'], '', '', '税号:', row['tax_id'],
                          '', '银行账户:', 'private-bank-account']
            body = json.dumps({'htmlTag': '<table><tr>' + ''.join(
                f'<td><div>{escape(value)}</div></td>' for value in values
            ) + '</tr></table>', 'totalPage': 1, 'currentPage': 1})
        response = Response(body.encode())
        self.responses.append(response)
        return response
