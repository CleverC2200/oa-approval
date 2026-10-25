from __future__ import annotations

from tests.approvals.source_fakes import POLICY

import json
import signal
from types import SimpleNamespace

import pytest

from apps.oa.approvals.collection import downloads as attachments
from apps.oa.approvals.sources import downloads as remote
from apps.oa.approvals.sources import travel as travel_orders
from apps.oa.approvals.sources import travel_hotel

travel_source = travel_orders.TravelSource(POLICY)


def expense_order(source='3', order_no='BL-SYNTHETIC'):
    return {'order_no': order_no, 'expense_description': '合成住宿',
            'sjly': source, 'id': 'expense-1', 'bxpch': 'batch-1'}


def expense_result(**changes):
    return {'id': 'expense-1', 'ywdh': 'BL-SYNTHETIC', 'bxpch': 'batch-1', 'sjly': '3',
            'cpbh': '0300', 'ddlx': '03001', 'xcrqs': '2026-10-20 00:00:00',
            'xcrqz': '2026-10-22 00:00:00', 'ddcs': '合成市', 'jdmc': '',
            'fkFymxKzbBean': {'jddz': ''}, 'jdmxList': [{'rzrq': '2026-10-20'}], **changes}


def native_attachment_rows():
    return [
        {'id': 'expense-a', 'fjList': [{'id': 'attachment-a', 'fjdz': '', 'fjmc': '',
                                      'fjlx': '', 'fjdx': '', 'wbfjdz': '', 'fjwb': ''}]},
        {'id': 'expense-b', 'fjList': [{'id': 'attachment-b', 'fjdz': '', 'fjmc': '',
                                      'fjlx': '', 'fjdx': '', 'wbfjdz': '', 'fjwb': ''}]},
        {'id': 'expense-c', 'fjList': [{'id': f'attachment-{index}', 'fjdz': f'{100 + index}/file.txt',
                                      'fjmc': 'file.txt'} for index in range(4)],
         'fpList': [{'id': f'invoice-{index}'} for index in range(9)]},
    ]


def test_native_attachment_records_are_distinct_from_downloads_and_invoices(browser_transport):
    browser_transport.outcomes[True] = {'expense_rows': native_attachment_rows()}
    evidence = collect_travel(count='6')
    assert evidence['errors'] == []
    assert len(evidence['resources']) == 4
    inventory = evidence['attachment_scan']['inventory']
    assert len(inventory) == 6
    assert [item['reason'] for item in inventory].count('empty_placeholder') == 2
    assert {item['owner_ref'] for item in inventory} == {'expense-a', 'expense-b', 'expense-c'}
    assert all('invoice' not in json.dumps(item) for item in inventory)


@pytest.mark.parametrize('change', [{'fjmc': 'missing.txt'}, {'fjdx': '120'},
                                   {'wbfjdz': 'https://other.example.test/private?token=synthetic-secret'},
                                   {'fjdz': 'not-a-supported-address'}])
def test_unreadable_attachment_record_is_not_an_empty_placeholder(browser_transport, change):
    rows = native_attachment_rows()
    rows[0]['fjList'][0].update(change)
    browser_transport.outcomes[True] = {'expense_rows': rows}
    evidence = collect_travel(count='6')
    assert evidence['errors'][0]['code'] == 'travel_attachment_resources_incomplete'
    diagnostic = next(item for item in evidence['errors'][0]['diagnostics']
                      if item.get('attachment_ref') == 'attachment-a')
    assert diagnostic['reason'] in {'resource_address_missing', 'resource_address_invalid'}
    assert 'synthetic-secret' not in json.dumps(evidence['errors'])


def test_repeated_native_responses_do_not_inflate_attachment_record_count(browser_transport):
    rows = native_attachment_rows()
    browser_transport.outcomes[True] = {'expense_rows': rows + rows}
    evidence = collect_travel(count='6')
    assert len(evidence['attachment_scan']['inventory']) == 6
    assert len(evidence['resources']) == 4


def test_wrong_candidate_cannot_supply_attachments_to_the_target_page(browser_transport, monkeypatch):
    wrong_url = 'https://trip.example.test/wrong'
    target_url = 'https://trip.example.test/target'
    browser_transport.candidate_outcomes = {
        wrong_url: {
            'text': 'SYNTHETIC-OTHER-REQUEST',
            'expense_rows': native_attachment_rows() + [{
                'cpbh': '0300', 'ddlx': '03001', 'ywdh': 'BL-SYNTHETIC',
                'fysm': '合成住宿', 'sjly': '3', 'id': 'expense-1', 'bxpch': 'batch-1',
            }],
            'hotel_payload': {'success': True, 'result': expense_result()},
        },
        target_url: {'text': 'SYNTHETIC-TRAVEL 暂无数据'},
    }
    monkeypatch.setattr(travel_orders.TravelSource, '_oa_jump_url', staticmethod(lambda _: wrong_url))
    monkeypatch.setattr(travel_orders.TravelSource, '_trip_url', staticmethod(lambda _: target_url))
    candidates = []
    read = travel_source._read_trip_candidate_results

    def capture(*args, **kwargs):
        result = read(*args, **kwargs)
        candidates.extend(result)
        return result

    monkeypatch.setattr(travel_orders.TravelSource, '_read_trip_candidate_results', staticmethod(capture))
    evidence = collect_travel(count='6')
    wrong, target = candidates
    assert len(wrong['attachment_inventory']) == 6
    assert wrong['hotel_order_details'][0]['order_detail'] == expense_result()
    assert target['attachment_inventory'] == []
    assert target['resources'] == target['hotel_order_details'] == target['diagnostics'] == []
    assert evidence['resources'] == []
    assert not any(field['name'] == '商旅住宿订单详情' for field in evidence['fields'])
    assert 'inventory' not in evidence.get('attachment_scan', {})
    assert all(page.closed for browser in browser_transport.browsers for page in browser.pages)

    from apps.oa.approvals.collection import case as case_bundle

    config = {'flow_key': 'travel_expense', 'workflow_id': '138023', 'workflow_name': '差旅费报销流程'}
    workflow = {'requestId': '12', 'workflowBaseInfo': {
        'workflowId': config['workflow_id'], 'workflowName': config['workflow_name']},
        'workflowMainTableInfo': {'requestRecords': [{'workflowRequestTableFields': [
            {'fieldName': 'lcbh', 'fieldShowName': '流程编号', 'fieldShowValue': 'SYNTHETIC-TRAVEL'},
            {'fieldName': 'fjsl', 'fieldShowName': '附件数量', 'fieldShowValue': '6'},
        ]}]}}
    oa_api = SimpleNamespace(request_context=lambda _: (workflow, []))
    monkeypatch.setattr(case_bundle, '_authority_facts', lambda **_: [])
    monkeypatch.setattr(case_bundle.invoice_originals, 'resources', lambda *_, **__: [])
    with pytest.raises(case_bundle.CollectionFailure, match='^travel_attachment_resources_incomplete$') as caught:
        case_bundle.collect_case('12', oa_api=oa_api, configs=[config], allow_browser_download=True,
                                 attachment_timeout_sec=1, source_policy=POLICY)
    assert caught.value.counts['trusted_source_sighting_count'] == 0


def hotel_transport(payload, status=200):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status=status, json=lambda: payload)

    return SimpleNamespace(request=SimpleNamespace(post=post)), calls


def test_hotel_batch_keeps_each_protocol_request_and_semantics():
    online = {'ddbh': 'online-1', 'jddz': '合成线上地址', 'extra': {'rows': [1, 2]}}
    offline = expense_result(fkFymxKzbBean={'jddz': '合成申报地址'})
    payloads = iter([{'result': online}, {'success': True, 'result': offline}])
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        payload = next(payloads)
        return SimpleNamespace(status=200, json=lambda: payload)

    details = travel_hotel.read_details(
        SimpleNamespace(post=post),
        [{'order_no': 'online-1', 'expense_description': '线上住宿', 'sjly': '2'}, expense_order()],
        origin=POLICY.travel_origin, timeout_sec=1,
    )

    assert calls[0][0] == POLICY.travel_origin + '/hotel/fcc/hotel/order/b2ghotelzcd/getOrderDetail?ddbh=online-1'
    assert 'data' not in calls[0][1]
    assert calls[1][0] == POLICY.travel_origin + '/fcecf/fcc/pcapi/COMM_B2G_bldDetail'
    assert calls[1][1]['data'] == {'id': 'expense-1', 'ywdh': 'BL-SYNTHETIC', 'bxpch': 'batch-1'}
    assert all(options['max_redirects'] == 0 for _, options in calls)
    assert [detail['date_semantics'] for detail in details] == ['reserved', 'declared']
    assert 'location_semantics' not in details[0]
    assert details[1]['location_semantics'] == 'declared'
    assert [detail['order_detail'] for detail in details] == [online, offline]


def test_hotel_batch_budget_precedes_later_missing_identity(monkeypatch):
    ticks = iter([0.0, 0.1, 2.0])
    monkeypatch.setattr(travel_hotel.time, 'monotonic', lambda: next(ticks, 2.0))
    context, calls = hotel_transport({'result': {'ddbh': 'online-1'}})
    missing_identity = expense_order()
    del missing_identity['id']

    details = travel_hotel.read_details(
        context.request,
        [{'order_no': 'online-1', 'sjly': '2'}, missing_identity],
        origin=POLICY.travel_origin, timeout_sec=1,
    )

    assert len(calls) == 1
    assert calls[0][1]['timeout'] == 900
    assert details[0]['read_status'] == 'read'
    assert details[1] == {
        **missing_identity, 'read_status': 'failed',
        'reason': 'hotel_order_detail_timeout', 'diagnostics': {'stage': 'hotel_detail'},
    }


def test_hotel_discovery_keeps_native_expense_source_and_identity():
    row = {'cpbh': '0300', 'ddlx': '03001', 'ywdh': 'BL-SYNTHETIC',
           'fysm': '合成住宿', 'sjly': '3', 'id': 'expense-1', 'bxpch': 'batch-1'}
    assert travel_hotel.orders_from_expenses({'result': [row, row]}) == [expense_order()]


@pytest.mark.parametrize('source', ['1', '3', '4', '5'])
def test_offline_hotel_uses_native_expense_read_and_keeps_declared_semantics(source):
    result = expense_result(sjly=source)
    context, calls = hotel_transport({'success': True, 'result': result})
    detail, = travel_hotel.read_details(context.request, [expense_order(source)], origin=POLICY.travel_origin, timeout_sec=1)

    assert calls[0][0].endswith('/fcecf/fcc/pcapi/COMM_B2G_bldDetail')
    assert calls[0][1]['data'] == {'id': 'expense-1', 'ywdh': 'BL-SYNTHETIC', 'bxpch': 'batch-1'}
    assert calls[0][1]['max_redirects'] == 0
    assert detail['read_status'] == 'read'
    assert detail['detail_source'] == 'offline_expense'
    assert detail['date_semantics'] == 'declared'
    assert detail['check_in'] == result['xcrqs']
    assert detail['check_out'] == result['xcrqz']
    assert detail['hotel_name'] == detail['hotel_address'] == detail['district'] == ''
    assert detail['order_detail'] == result


def test_order_number_prefix_does_not_choose_hotel_interface():
    context, calls = hotel_transport({'success': True, 'result': {'ddbh': 'BL-SYNTHETIC'}})
    detail, = travel_hotel.read_details(context.request, [expense_order('2')], origin=POLICY.travel_origin, timeout_sec=1)
    assert travel_hotel.HOTEL_ORDER_DETAIL_PATH in calls[0][0]
    assert detail['detail_source'] == 'online_order'


@pytest.mark.parametrize('key', ['id', 'ywdh', 'bxpch', 'sjly', 'cpbh', 'ddlx'])
def test_offline_hotel_rejects_wrong_expense_identity(key):
    context, _ = hotel_transport({'success': True, 'result': expense_result(**{key: 'other'})})
    detail, = travel_hotel.read_details(context.request, [expense_order()], origin=POLICY.travel_origin, timeout_sec=1)
    assert detail['reason'] == 'hotel_order_identity_mismatch'
    assert 'order_detail' not in detail


def test_offline_hotel_without_expense_identity_does_not_guess_or_request():
    context, calls = hotel_transport({'success': True, 'result': expense_result()})
    order = expense_order()
    del order['id']
    detail, = travel_hotel.read_details(context.request, [order], origin=POLICY.travel_origin, timeout_sec=1)
    assert detail['reason'] == 'hotel_expense_identity_missing'
    assert calls == []


@pytest.mark.parametrize('status,payload,reason', [
    (401, {}, 'hotel_order_authentication_failed'),
    (302, {}, 'hotel_order_redirected'),
    (503, {}, 'hotel_order_http_failed'),
    (200, {'success': False, 'result': None}, 'hotel_order_api_failed'),
    (200, {'success': True, 'result': None}, 'hotel_order_result_missing'),
    (200, {'success': True, 'result': []}, 'hotel_order_result_invalid'),
    (200, {'success': True, 'result': {}}, 'hotel_order_identity_missing'),
    (200, {'success': True, 'result': {'ddbh': 'other'}}, 'hotel_order_identity_mismatch'),
])
def test_hotel_failures_preserve_distinct_safe_diagnostics(status, payload, reason):
    payload['message'] = 'synthetic-secret-message'
    context, _ = hotel_transport(payload, status)
    detail, = travel_hotel.read_details(context.request, [expense_order('2')], origin=POLICY.travel_origin, timeout_sec=1)
    assert detail['reason'] == reason
    assert detail['diagnostics']['http_status'] == status
    assert 'synthetic-secret-message' not in json.dumps(detail)
    assert 'order_detail' not in detail


def test_hotel_transport_exception_keeps_type_without_request_secrets():
    def post(*_args, **_kwargs):
        raise TimeoutError('https://example.test/?token=synthetic-secret')

    context = SimpleNamespace(request=SimpleNamespace(post=post))
    detail, = travel_hotel.read_details(context.request, [expense_order('2')], origin=POLICY.travel_origin, timeout_sec=1)
    assert detail['reason'] == 'hotel_order_detail_timeout'
    assert detail['diagnostics']['exception_type'] == 'TimeoutError'
    assert 'synthetic-secret' not in json.dumps(detail)


@pytest.mark.parametrize('success', [None, 'false'])
def test_offline_hotel_requires_native_success_flag(success):
    payload = {'result': expense_result()}
    if success is not None:
        payload['success'] = success
    context, _ = hotel_transport(payload)
    detail, = travel_hotel.read_details(context.request, [expense_order()], origin=POLICY.travel_origin, timeout_sec=1)
    assert detail['reason'] == 'hotel_order_response_invalid'
    assert 'order_detail' not in detail


@pytest.mark.parametrize('address,identity', [('合成路1号', 'hotel-1'), ('', 'hotel-1'), ('合成路1号', 'other')])
def test_hotel_details_preserve_complete_result_and_identity(address, identity):
    result = {'ddbh': identity, 'jdmc': '合成酒店', 'jddz': address, 'jdcszwmc': '杭州市',
              'xzqmc': '西湖区', 'ydrzrq': '2026-10-20', 'ydldrq': '2026-10-21', 'ddztmc': '已完成'}
    context = SimpleNamespace(request=SimpleNamespace(post=lambda *_a, **_k: SimpleNamespace(
        json=lambda: {'result': result})))
    details = travel_hotel.read_details(
        context.request, [{'order_no': 'hotel-1', 'expense_description': '合成住宿'}], origin=POLICY.travel_origin, timeout_sec=1)
    assert len(details) == 1
    if identity == 'hotel-1':
        assert details[0]['order_detail'] == result
        assert details[0]['read_status'] == 'read'
    else:
        assert details[0]['read_status'] == 'failed'
        assert details[0]['reason'] == 'hotel_order_identity_mismatch'
        assert 'order_detail' not in details[0]


@pytest.fixture
def browser_transport(monkeypatch):
    import playwright.sync_api

    transport = SimpleNamespace(launches=[], browsers=[], outcomes={}, exits=[], requests=[])

    class Page:
        frames = []
        main_frame = None

        def __init__(self, headless):
            self.outcome = transport.outcomes.get(headless, {})
            self.url = ''
            self.handlers = []
            self.closed = False

        def on(self, _event, handler):
            self.handlers.append(handler)

        def goto(self, url, **_kwargs):
            self.url = url
            self.outcome = getattr(transport, 'candidate_outcomes', {}).get(url, self.outcome)
            failure = self.outcome.get('failure')
            if failure == 'deadline':
                signal.raise_signal(signal.SIGALRM)
            if failure == 'read':
                raise RuntimeError('synthetic navigation failure')
            if self.outcome.get('expense_rows'):
                response = SimpleNamespace(url=POLICY.travel_origin + travel_orders.TRIP_EXPENSE_API_PATH,
                    status=200, json=lambda: {'success': True, 'result': self.outcome['expense_rows']})
                for handler in self.handlers:
                    handler(response)

        def locator(self, *_args, **_kwargs):
            return self

        get_by_role = locator
        get_by_text = locator

        def inner_text(self, **_kwargs):
            return self.outcome.get('text', 'SYNTHETIC-TRAVEL 暂无数据')

        def content(self):
            return self.outcome.get('markup', '<html></html>')

        def count(self):
            return 1

        def nth(self, _index):
            return self

        def is_visible(self):
            return True

        def click(self, **_kwargs):
            pass

        def wait_for_timeout(self, _milliseconds):
            pass

        def close(self):
            self.closed = True

    class Browser:
        def __init__(self, headless):
            self.headless = headless
            self.pages = []
            self.disposed = False
            self.close_timers = []

        def new_context(self, **_kwargs):
            def new_page():
                self.page = Page(self.headless)
                self.pages.append(self.page)
                return self.page

            def post(url, **kwargs):
                transport.requests.append((url, kwargs))
                return SimpleNamespace(status=200, json=lambda: self.page.outcome['hotel_payload'])

            return SimpleNamespace(new_page=new_page, cookies=lambda *_a: [],
                                   request=SimpleNamespace(post=post))

        def close(self):
            self.close_timers.append(signal.getitimer(signal.ITIMER_REAL)[0])
            self.disposed = True

    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=self.launch))

        def launch(self, *, headless, **_kwargs):
            transport.launches.append(headless)
            browser = Browser(headless)
            transport.browsers.append(browser)
            return browser

        def __exit__(self, *_args):
            transport.exits.append(signal.getitimer(signal.ITIMER_REAL)[0])
            for browser in transport.browsers:
                browser.disposed = True

    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', Playwright)
    monkeypatch.setattr(travel_orders.TravelSource, '_ensure_cdp_available', staticmethod(lambda: True))
    monkeypatch.setattr(remote.DownloadTransfer, 'rpa_storage_state', staticmethod(lambda **_k: {'cookies': [], 'origins': []}))
    monkeypatch.setattr(remote, 'browser_visible_fallback_enabled', lambda: True)
    return transport


def collect_travel(count='0'):
    return travel_source.collect_evidence(
        {'flow_key': 'travel_expense'},
        {'workflowMainTableInfo': {'requestRecords': [{'workflowRequestTableFields': [
            {'fieldName': 'lcbh', 'fieldShowName': '流程编号', 'fieldShowValue': 'SYNTHETIC-TRAVEL'},
            {'fieldName': 'fjsl', 'fieldShowName': '附件数量', 'fieldShowValue': count},
        ]}]}},
        '12', allow_browser_download=True, timeout_sec=1,
    )


def test_complete_empty_travel_scan_does_not_open_visible_browser(browser_transport):
    evidence = collect_travel()

    assert evidence['attachment_scan']['status'] == 'empty'
    assert browser_transport.launches == [True]


def test_complete_empty_travel_scan_without_count_does_not_open_visible_browser(browser_transport):
    evidence = collect_travel(count='')

    assert evidence['attachment_scan']['status'] == 'empty'
    assert browser_transport.launches == [True]


@pytest.mark.parametrize('failure', ['', 'read', 'deadline'])
def test_travel_browser_disposes_owned_resources_after_read_timer_is_removed(browser_transport, failure):
    browser_transport.outcomes = {mode: {'failure': failure} for mode in (True, False)}
    previous_handler = signal.getsignal(signal.SIGALRM)
    evidence = collect_travel()

    if failure:
        assert evidence['errors'][0]['code'] == 'travel_attachment_discovery_failed'
        assert browser_transport.launches == [True, False]
    else:
        assert evidence['attachment_scan']['status'] == 'empty'
    assert all(browser.disposed for browser in browser_transport.browsers)
    assert browser_transport.exits == [0.0] * len(browser_transport.launches)
    assert all(not browser.close_timers for browser in browser_transport.browsers)
    assert signal.getsignal(signal.SIGALRM) == previous_handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@pytest.mark.parametrize('outcome', [
    {'text': 'SYNTHETIC-OTHER-REQUEST 暂无数据'},
    {'failure': 'read'},
    {'markup': '<a href="https://trip.example.test/opera/getFile/123/synthetic.pdf">synthetic.pdf</a>'},
])
def test_incomplete_headless_travel_scan_can_still_use_visible_fallback(browser_transport, outcome):
    browser_transport.outcomes[True] = outcome

    evidence = collect_travel()

    assert evidence['errors'] == []
    assert browser_transport.launches == [True, False]


def test_complete_nonempty_travel_scan_does_not_open_visible_browser(browser_transport):
    browser_transport.outcomes[True] = {
        'markup': '<a href="https://trip.example.test/opera/getFile/123/synthetic.pdf">synthetic.pdf</a>',
    }

    evidence = collect_travel(count='')

    assert [resource['id'] for resource in evidence['resources']] == ['travel_platform:123']
    assert browser_transport.launches == [True]


@pytest.mark.parametrize('address', ['', '合成市合成区合成路1号'])
def test_native_offline_source_flows_from_browser_response_to_bound_evidence(browser_transport, address):
    result = expense_result(fkFymxKzbBean={'jddz': address})
    browser_transport.outcomes[True] = {
        'expense_rows': [{'cpbh': '0300', 'ddlx': '03001', 'ywdh': 'BL-SYNTHETIC',
                         'fysm': '合成住宿', 'sjly': '3', 'id': 'expense-1', 'bxpch': 'batch-1'}],
        'hotel_payload': {'success': True, 'result': result},
    }
    evidence = collect_travel()
    fact, = [item for item in evidence['fields'] if item['name'] == '商旅住宿订单详情']
    detail = json.loads(fact['value'])
    assert detail['read_status'] == 'read' and detail['date_semantics'] == 'declared'
    assert detail['order_detail'] == result
    assert detail['hotel_address'] == address and detail['location_semantics'] == 'declared'
    assert fact['subject_ref'] == 'travel_order:BL-SYNTHETIC'
    assert not any(item['name'] == '酒店地址' for item in evidence['fields'])
    assert browser_transport.requests[0][0].endswith('/fcecf/fcc/pcapi/COMM_B2G_bldDetail')
    assert browser_transport.launches == [True]


@pytest.mark.parametrize('outcome,reason', [
    ({'failure': 'read'}, 'navigation_failed'),
    ({'failure': 'deadline'}, 'browser_deadline'),
    ({'text': '登录'}, 'login_required'),
    ({'text': 'SYNTHETIC-OTHER'}, 'flow_marker_mismatch'),
    ({'text': ''}, 'page_empty'),
    ({'text': 'SYNTHETIC-TRAVEL 系统正在处理中，请稍后'}, 'page_pending'),
])
def test_travel_failure_diagnostics_survive_browser_fallback(browser_transport, outcome, reason):
    browser_transport.outcomes = {mode: outcome for mode in (True, False)}
    evidence = collect_travel()
    error, = evidence['errors']
    assert error['code'] == 'travel_attachment_discovery_failed'
    assert reason in {item['reason'] for item in error['diagnostics']}
    assert {item['browser'] for item in error['diagnostics']} == {'headless', 'visible'}
    assert 'source_url' not in json.dumps(error)
