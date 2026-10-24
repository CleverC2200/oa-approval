from apps.oa_approval import invoice_buyers


CONFIG = {
    'flow_key': 'tpm_small_marketing_fee',
    'workflow_id': '195039',
    'workflow_name': 'TPM小额市场费用报销申请流程',
}


NUMBER = '00000000000000000101'


def linked_field(data_id='101', number=NUMBER):
    return {'fieldName': 'glfp', 'fieldShowName': '关联发票',
            'fieldValue': data_id, 'fieldShowValue': number}


class InvoiceApi:
    def __init__(self, rows=None, config=None):
        self.config = config or CONFIG
        self.rows = rows or [[linked_field()]]

    def request_context(self, _request_id):
        return {'workflowBaseInfo': {
            'workflowId': self.config['workflow_id'],
            'workflowName': self.config['workflow_name'],
        }, 'workflowDetailTableInfos': [{'workflowRequestTableRecords': [
            {'workflowRequestTableFields': fields} for fields in self.rows
        ]}]}, []

    def get(self, path, params=None):
        if path == invoice_buyers.TOKEN_PATH:
            return {'status': '1', 'token': 'synthetic-token'}
        if path == invoice_buyers.MAPPING_PATH:
            return {'status': '1', 'targetIds': 'cloud-' + params['ids']}
        raise AssertionError('Unexpected external API')


