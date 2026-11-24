from __future__ import annotations

from tests.approvals.source_fakes import POLICY

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import signal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.oa.approvals.collection import downloads as attachments
from apps.oa.approvals.operations import Approval
from apps.oa.approvals.source_policy import TravelReadPolicy
from apps.oa.approvals.sources import downloads as remote
from apps.oa.approvals.sources import travel as travel_orders
from apps.oa.approvals.sources import travel_hotel, travel_browser
from apps.oa.approvals.sources import travel_page_compat as compat, travel_read_session as sessions
from tests.approvals.collection.test_empty_attachments import TRAVEL_CONFIG
from tests.approvals.test_operations import FakeOA

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
    assert evidence.errors == ()
    assert len(evidence.download_targets) == 4
    inventory = evidence.attachment_scan_record()['inventory']
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
    assert evidence.errors[0].code == 'travel_attachment_resources_incomplete'
    diagnostic = next(item for item in evidence.errors[0].diagnostics
                      if item.get('attachment_ref') == 'attachment-a')
    assert diagnostic['reason'] in {'resource_address_missing', 'resource_address_invalid'}
    assert 'synthetic-secret' not in json.dumps([error.diagnostics for error in evidence.errors])


def test_repeated_native_responses_do_not_inflate_attachment_record_count(browser_transport):
    rows = native_attachment_rows()
    browser_transport.outcomes[True] = {'expense_rows': rows + rows}
    evidence = collect_travel(count='6')
    assert len(evidence.attachment_scan_record()['inventory']) == 6
    assert len(evidence.download_targets) == 4


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
    read = travel_source.browser.read_attempt

    def capture(*args, **kwargs):
        result = read(*args, **kwargs)
        candidates.extend(result)
        return result

    monkeypatch.setattr(travel_browser.BrowserAdapter, 'read_attempt', staticmethod(capture))
    evidence = collect_travel(count='6')
    wrong, target = candidates
    assert len(wrong.inventory) == 6
    assert wrong.hotel_details[0]['order_detail'] == expense_result()
    assert target.inventory == ()
    assert target.download_targets == target.hotel_details == target.diagnostics == ()
    assert evidence.download_targets == ()
    assert not any(field['name'] == '商旅住宿订单详情' for field in evidence.facts)
    assert 'inventory' not in (evidence.attachment_scan_record() or {})
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

    details = list(travel_hotel.read_details(
        SimpleNamespace(post=post),
        [{'order_no': 'online-1', 'expense_description': '线上住宿', 'sjly': '2'}, expense_order()],
        origin=POLICY.travel_origin, timeout_sec=1,
    ))

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

    details = list(travel_hotel.read_details(
        context.request,
        [{'order_no': 'online-1', 'sjly': '2'}, missing_identity],
        origin=POLICY.travel_origin, timeout_sec=1,
    ))

    assert len(calls) == 1
    assert calls[0][1]['timeout'] == 900
    assert details[0]['read_status'] == 'read'
    assert details[1] == {
        **missing_identity, 'read_status': 'failed', 'detail_source': 'offline_expense',
        'reason': 'hotel_order_detail_timeout', 'diagnostics': {'stage': 'hotel_detail', 'elapsed_ms': 0},
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
    details = list(travel_hotel.read_details(
        context.request, [{'order_no': 'hotel-1', 'expense_description': '合成住宿'}], origin=POLICY.travel_origin, timeout_sec=1))
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
            self.url = self.outcome.get('redirect', url)
            failure = self.outcome.get('failure')
            assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
            if failure == 'deadline':
                raise playwright.sync_api.TimeoutError('synthetic navigation deadline')
            if failure == 'read':
                raise RuntimeError('synthetic navigation failure')
            response = SimpleNamespace(url=POLICY.travel_origin + travel_browser.TRIP_EXPENSE_API_PATH,
                status=200, json=lambda: {'success': True, 'result': self.outcome.get('expense_rows', [])})
            for handler in self.handlers:
                handler(response)

        def locator(self, *_args, **_kwargs):
            return self

        get_by_role = locator
        get_by_text = locator

        def inner_text(self, **_kwargs):
            return self.outcome.get('text', 'SYNTHETIC-TRAVEL 暂无数据')

        def content(self):
            self.scanned_content = True
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
            if self.outcome.get('scan_failure') and hasattr(self, 'scanned_content'):
                raise RuntimeError('synthetic private scan failure')

        def close(self):
            self.closed = True
            if self.outcome.get('close_failure'):
                raise RuntimeError('synthetic page cleanup failure')

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
                reply = self.page.outcome.get('hotel_response')
                if reply is not None:
                    payload = reply(url)
                    return SimpleNamespace(status=200, json=lambda: payload)
                return SimpleNamespace(status=200, json=lambda: self.page.outcome['hotel_payload'])

            return SimpleNamespace(new_page=new_page, cookies=lambda *_a: getattr(transport, 'cookies', []),
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
            getattr(transport, 'launched', lambda: None)()
            transport.browsers.append(browser)
            return browser

        def __exit__(self, *_args):
            transport.exits.append(signal.getitimer(signal.ITIMER_REAL)[0])
            for browser in transport.browsers:
                browser.disposed = True

    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', Playwright)
    monkeypatch.setattr(travel_browser.BrowserAdapter, 'ensure_available', staticmethod(lambda: True))
    monkeypatch.setattr(remote.DownloadTransfer, 'rpa_storage_state', staticmethod(lambda **_k: {'cookies': [], 'origins': []}))
    monkeypatch.setattr(remote, 'browser_visible_fallback_enabled', lambda: True)
    transport.page_type = Page
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

    assert evidence.attachment_scan_record()['status'] == 'empty'
    assert browser_transport.launches == [True]


def test_complete_empty_travel_scan_without_count_does_not_open_visible_browser(browser_transport):
    evidence = collect_travel(count='')

    assert evidence.attachment_scan_record()['status'] == 'empty'
    assert browser_transport.launches == [True]


@pytest.mark.parametrize('failure', ['', 'read', 'deadline'])
def test_travel_browser_disposes_owned_resources_after_read_timer_is_removed(browser_transport, failure):
    browser_transport.outcomes = {mode: {'failure': failure} for mode in (True, False)}
    previous_handler = signal.getsignal(signal.SIGALRM)
    evidence = collect_travel()

    if failure:
        assert evidence.errors[0].code == 'travel_attachment_discovery_failed'
        assert browser_transport.launches == [True, False]
    else:
        assert evidence.attachment_scan_record()['status'] == 'empty'
    assert all(browser.disposed for browser in browser_transport.browsers)
    assert browser_transport.exits == [0.0] * len(browser_transport.launches)
    assert all(browser.close_timers == [0.0] for browser in browser_transport.browsers)
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

    assert evidence.errors == ()
    assert browser_transport.launches == [True, False]


@pytest.mark.parametrize('visible_address', ['合成酒店地址', '合成更正地址'])
def test_visible_fallback_deduplicates_hotel_evidence_without_merging_different_results(
        browser_transport, monkeypatch, visible_address):
    real_monotonic = travel_hotel.time.monotonic
    applied_delay = 0.0

    def response(url):
        nonlocal applied_delay
        order = parse_qs(urlsplit(url).query)['ddbh'][0]
        applied_delay += 0.01 if browser_transport.launches[-1] else 0.06
        address = '合成酒店地址' if browser_transport.launches[-1] else visible_address
        return {'result': {'ddbh': order, 'jddz': address}}

    monkeypatch.setattr(travel_hotel.time, 'monotonic', lambda: real_monotonic() + applied_delay)
    outcomes = {'expense_rows': [{'cpbh': '0300', 'ddlx': '03001', 'sjly': '2', 'ywdh': 'hotel-1'}],
                'hotel_response': response}
    browser_transport.outcomes = {False: outcomes, True: {
        **outcomes, 'markup': '<a href="https://trip.example.test/opera/getFile/123/synthetic.pdf">synthetic.pdf</a>',
    }}
    evidence = collect_travel()
    assert evidence.errors == ()
    assert browser_transport.launches == [True, False]
    details = [json.loads(fact['value']) for fact in evidence.facts if fact['name'] == '商旅住宿订单详情']
    addresses = [fact['value'] for fact in evidence.facts if fact['name'] == '酒店地址']
    assert addresses == list(dict.fromkeys(['合成酒店地址', visible_address]))
    assert [detail['hotel_address'] for detail in details] == addresses
    assert 10 <= details[0]['diagnostics']['elapsed_ms'] < 60


def test_missing_hotel_order_identity_has_a_safe_diagnostic(browser_transport):
    browser_transport.outcomes[True] = {'expense_rows': [
        {'cpbh': '0300', 'ddlx': '03001', 'sjly': '2', 'fysm': 'private expense description'},
    ]}
    evidence = collect_travel()
    assert evidence.errors == ()
    diagnostic, = evidence.attachment_scan_record()['diagnostics']
    assert diagnostic.pop('elapsed_ms') >= 0
    assert diagnostic == {
        'stage': 'hotel_detail', 'source_path': 'hotel_details[0]', 'browser': 'headless',
        'read_status': 'failed', 'reason': 'hotel_order_identity_missing',
    }
    assert browser_transport.requests == []
    assert not any(fact['name'] == '商旅住宿订单详情' for fact in evidence.facts)


def test_complete_nonempty_travel_scan_does_not_open_visible_browser(browser_transport):
    browser_transport.outcomes[True] = {
        'markup': '<a href="https://trip.example.test/opera/getFile/123/synthetic.pdf">synthetic.pdf</a>',
    }

    evidence = collect_travel(count='')

    assert [resource['id'] for resource in evidence.download_targets] == ['travel_platform:123']
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
    fact, = [item for item in evidence.facts if item['name'] == '商旅住宿订单详情']
    detail = json.loads(fact['value'])
    assert detail['read_status'] == 'read' and detail['date_semantics'] == 'declared'
    assert detail['order_detail'] == result
    assert detail['hotel_address'] == address and detail['location_semantics'] == 'declared'
    assert fact['subject_ref'] == 'travel_order:BL-SYNTHETIC'
    assert not any(item['name'] == '酒店地址' for item in evidence.facts)
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
    error, = evidence.errors
    assert error.code == 'travel_attachment_discovery_failed'
    assert reason in {item['reason'] for item in error.diagnostics}
    assert {item['browser'] for item in error.diagnostics} == {'headless', 'visible'}
    assert 'source_url' not in json.dumps(error.diagnostics)


def test_completed_candidate_survives_page_cleanup_failure(browser_transport):
    browser_transport.outcomes[True] = {'close_failure': True}
    evidence = collect_travel()
    assert evidence.errors == ()
    assert evidence.attachment_scan_record()['status'] == 'empty'
    assert browser_transport.launches == [True]
    assert browser_transport.exits == [0.0]
    assert browser_transport.browsers[0].pages[0].closed


def test_download_cookie_stays_out_of_scan_and_facts(browser_transport):
    browser_transport.outcomes[True] = {'expense_rows': native_attachment_rows()}
    browser_transport.cookies = [{'domain': 'trip.example.test', 'name': 'session',
                                  'value': 'synthetic-cookie-secret'}]
    evidence = collect_travel(count='6')
    assert all(target['_ephemeral_cookie_header'] == 'session=synthetic-cookie-secret'
               for target in evidence.download_targets)
    assert 'synthetic-cookie-secret' not in json.dumps(evidence.attachment_scan_record())
    assert 'synthetic-cookie-secret' not in json.dumps(evidence.facts)
    evidence.require_request_binding('12')
    with pytest.raises(RuntimeError, match='resource_ownership_untrusted'):
        evidence.require_request_binding('other')


@pytest.mark.parametrize('message,reason', [
    ('非单点登录场景，请联系管理员', 'travel_sso_account_missing'),
    ('未绑定法人公司，请联系管理员', 'travel_sso_company_missing'),
    ('synthetic-provider-error', 'travel_sso_rejected'),
])
def test_sso_error_is_reported_without_scanning_or_leaking_redirect(browser_transport, monkeypatch, message, reason):
    from urllib.parse import urlencode

    url = POLICY.travel_origin + '/fcc/default/login/ssoError.html?' + urlencode(
        {'errorMsg': message, 'traceId': 'synthetic-private-trace'})
    browser_transport.outcomes[True] = {'redirect': url}
    browser_transport.outcomes[False] = {'redirect': url}
    monkeypatch.setattr(travel_browser, '_read_stable_body_text',
                        lambda *_a, **_k: pytest.fail('SSO rejection must stop before reading attachments'))
    evidence = collect_travel()
    error, = evidence.errors
    assert reason in {item['reason'] for item in error.diagnostics}
    assert not evidence.facts and not evidence.download_targets
    assert 'synthetic-private-trace' not in json.dumps(error.diagnostics)
    assert message not in json.dumps(error.diagnostics)
    assert all(browser.close_timers == [0.0] for browser in browser_transport.browsers)


@pytest.mark.parametrize('launch_seconds', [0.0, 6.0])
def test_late_read_preserves_browser_cleanup_budget(browser_transport, monkeypatch, tmp_path, launch_seconds):
    import time
    from apps.oa.runtime import collection_budget

    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    marker = tmp_path / 'phase.json'
    seen = []

    def launched():
        clock[0] = launch_seconds

    browser_transport.launched = launched

    def read_candidates(_attempt, _context):
        assert _attempt.deadline == 10.0
        assert clock[0] == launch_seconds
        clock[0] = 9.0
        browser = browser_transport.browsers[0]
        close = browser.close

        def finish():
            state = json.loads(marker.read_text())
            assert state['phase'] == 'travel_browser_cleanup'
            assert 10 < state['deadline'] - clock[0] <= 15
            seen.append(True)
            close()

        browser.close = finish

    monkeypatch.setattr(travel_browser._BrowserAttempt, '_read_candidates', read_candidates)
    with collection_budget.tracking(marker):
        travel_source.browser.read_attempt([], timeout_sec=10, storage_state={}, headless=True,
                                          expected_marker='SYNTHETIC-TRAVEL', expected_resource_count=0)
    assert seen == [True]
    assert json.loads(marker.read_text()) is None


@pytest.mark.parametrize('read_status', ['complete', 'incomplete', 'failed'])
def test_read_outcome_is_saved_before_browser_cleanup_blocks(browser_transport, monkeypatch, tmp_path, read_status):
    from dataclasses import replace
    from apps.oa.runtime import collection_budget

    class CollectorTerminated(BaseException):
        pass

    marker = tmp_path / 'phase.json'
    checkpoint = marker.with_suffix('.read.json')
    original_read = travel_browser._BrowserAttempt._read_candidates

    def close():
        record = json.loads(checkpoint.read_text())
        assert record['run_id'] == 'synthetic-run'
        assert record['task_id'] == 12 and record['agent_id'] == 'reviewer'
        result = record['read_results'][-1]
        assert result['status'] == read_status
        assert result['observations']
        if read_status == 'failed':
            assert result['observations'][0]['diagnostics'][0]['exception_type'] == 'RuntimeError'
            assert result['observations'][0]['read_error']['exception_type'] == 'RuntimeError'
            assert result['observations'][0]['read_error']['frames'][-1]['function'] == 'read_candidates'
        else:
            assert 'SYNTHETIC-TRAVEL' in result['observations'][0]['text']
        assert 'private-cookie' not in checkpoint.read_text()
        assert 'private-session-token' not in checkpoint.read_text()
        assert checkpoint.stat().st_mode & 0o077 == 0
        raise CollectorTerminated()

    def read_candidates(attempt, context):
        browser_transport.browsers[-1].close = close
        if read_status == 'failed':
            raise RuntimeError('private-session-token')
        original_read(attempt, context)
        attempt.results[-1] = replace(attempt.results[-1],
            scan_status='resources_found' if read_status == 'complete' else 'empty',
            download_targets=({'id': 'synthetic-file', '_ephemeral_cookie_header': 'private-cookie'},),
            order_invoices=({'invoices': [{'resource': {'_ephemeral_cookie_header': 'private-cookie',
                'id': 'synthetic-invoice'}}]},))
        attempt.expected_resource_count = 1

    monkeypatch.setattr(travel_browser._BrowserAttempt, '_read_candidates', read_candidates)
    with collection_budget.tracking(marker, run_id='synthetic-run', task_id=12, agent_id='reviewer'):
        with pytest.raises(CollectorTerminated):
            travel_source.browser.read_attempt(['https://trip.example.test/target'], timeout_sec=10,
                storage_state={}, headless=True, expected_marker='SYNTHETIC-TRAVEL', expected_resource_count=0)



@pytest.mark.parametrize('read_failed', [False, True])
def test_candidate_outcome_is_saved_before_page_cleanup_blocks(browser_transport, monkeypatch, tmp_path, read_failed):
    from apps.oa.runtime import collection_budget

    class CollectorTerminated(BaseException):
        pass

    marker = tmp_path / 'phase.json'
    original_enter = travel_browser._CandidateSession.__enter__

    def enter(candidate):
        def close():
            phase = json.loads(marker.read_text())
            assert phase['phase'] == 'travel_page_cleanup'
            record = json.loads(marker.with_suffix('.read.json').read_text())['read_results'][-1]
            assert record['scope'] == 'candidate'
            assert record['status'] == ('incomplete' if read_failed else 'complete')
            if read_failed:
                error = record['observations'][0]['read_error']
                assert error['exception_type'] == 'TimeoutError'
                assert error['frames'][-1]['function'] == 'goto'
                assert 'private-session-token' not in json.dumps(record)
            else:
                assert 'SYNTHETIC-TRAVEL' in record['observations'][0]['text']
            raise CollectorTerminated()

        def goto(*_args, **_kwargs):
            raise TimeoutError('private-session-token')

        candidate.page.close = close
        if read_failed:
            candidate.page.goto = goto
        return original_enter(candidate)

    monkeypatch.setattr(travel_browser._CandidateSession, '__enter__', enter)
    with collection_budget.tracking(marker):
        with pytest.raises(CollectorTerminated):
            travel_source.browser.read_attempt(['https://trip.example.test/target'], timeout_sec=10,
                storage_state={}, headless=True, expected_marker='SYNTHETIC-TRAVEL', expected_resource_count=0)


@pytest.fixture
def collected_hotels(tmp_path, monkeypatch, browser_transport):
    outcomes = browser_transport.outcomes[True] = {
        'expense_rows': [
            {'id': f'expense-{index}', 'ywdh': f'hotel-{index}', 'fysm': f'合成住宿 {index}',
             'sjly': '2', 'bxpch': 'batch-1', 'cpbh': '0300', 'ddlx': '03001'}
            for index in (1, 2)
        ],
    }
    failures = set()
    fault = {'after_orders': None, 'deadline_order': None, 'before_deadline': lambda: None}
    orders_read = []

    def hotel_response(url):
        order, = parse_qs(urlsplit(url).query)['ddbh']
        orders_read.append(order)
        if order == fault['deadline_order']:
            fault['before_deadline']()
            raise travel_browser._TravelReadDeadline('synthetic private deadline')
        if order in failures:
            raise TimeoutError('synthetic private detail')
        return {'success': True, 'result': {
            'ddbh': order, 'ydrzrq': '2026-10-20', 'ydldrq': '2026-10-22', 'jdcszwmc': '合成市',
        }}

    outcomes['hotel_response'] = hotel_response
    monkeypatch.setattr(sessions, 'api_storage_state', lambda *_a, **_kw: {'cookies': []})
    monkeypatch.setenv('OA_UID', '7')

    def start_browser(session, engine, _state, deadline, **_):
        rules = sessions.Policy(session.policy)
        records = []
        session.transport = SimpleNamespace(policy=rules, records=records, wait_for_idle=lambda **_: True)
        session.engine, session.version = engine, 'synthetic-browser'
        amounts = {field: 0 for field in ['cwspbzje', *compat.SUMMARY_FIELDS]}
        marker = 'SYNTHETIC-TRAVEL'
        reads_at_start = len(orders_read)
        trip_identity = {'id': '17', 'account': 'account-1', 'tenant': 'tenant-1'}

        def response(path, payload, status=200):
            request_id = f'r{len(records) + 1}'
            facts = {'path': path, 'request_id': request_id, 'status': status}
            records.extend({**facts, 'decision': event} for event in (
                'request_started', 'forwarded', 'client_response_complete',
            ))
            host = rules.oa_host if path == sessions.ACCOUNT_PATH else rules.trip_host
            return SimpleNamespace(url=f'https://{host}{path}', status=status, json=lambda: payload,
                                   headers={'x-oa-readonly-request-id': request_id})

        class Page(browser_transport.page_type):
            def goto(self, url, **_):
                self.url = url
                if url.endswith(sessions.ACCOUNT_PATH):
                    self.outcome = {'text': json.dumps({
                        'data': {'accountlist': [{'userid': '7', 'iscurrent': '1'}]},
                    })}
                    return response(sessions.ACCOUNT_PATH, {})
                self.url = f'https://{rules.trip_host}/fcc/fcecf/bxdHome/index.html'
                base = {'bxdzb': {'bxdh': marker, 'bxpch': 'batch-1', 'bxbt': '合成差旅',
                                 'bxrid': '17', 'tdrgh': 'account-1', 'qybh': 'tenant-1'},
                        'fkBxdKzb': {'cwspbzje': 0}}
                for path, result in ((compat.BASE_PATH, base),
                                     (compat.EXP_PATH, {'bxmxList': outcomes['expense_rows']}),
                                     (compat.SUMMARY_PATH, amounts)):
                    reply = response(path, {'success': True, 'result': result})
                    for handler in self.handlers:
                        handler(reply)
                return SimpleNamespace(status=200)

            def locator(self, selector, *_a, **_kw):
                return SimpleNamespace(evaluate=lambda *_: True, count=lambda: 1, nth=lambda _: self)                     if 'data-page="fjxx-page"' in selector else self

            def wait_for_function(self, *_a, **_kw):
                pass

            def evaluate(self, expression, arg=None):
                if 'await fetch(' in expression:
                    try:
                        body = hotel_response(arg['url'])
                    except TimeoutError:
                        request_id = f'r{len(records) + 1}'
                        facts = {'path': travel_hotel.HOTEL_ORDER_DETAIL_PATH, 'request_id': request_id}
                        records.extend([
                            {**facts, 'decision': 'request_started', 'phase': 'client_read'},
                            {**facts, 'decision': 'forwarded', 'phase': 'upstream', 'status': 200},
                            {**facts, 'decision': 'client_write_error', 'phase': 'client_write',
                             'error_type': 'BrokenPipeError'},
                        ])
                        return {'timed_out': True}
                    reply = response(travel_hotel.HOTEL_ORDER_DETAIL_PATH, body)
                    for handler in self.handlers:
                        handler(reply)
                    return {'status': reply.status, 'body': body}
                if 'let u=window.user' in expression:
                    if len(orders_read) - reads_at_start == 2 and fault['after_orders'] == 'identity':
                        return {**trip_identity, 'tenant': 'different'}
                    return trip_identity
                if 'window.bxdAppData' in expression:
                    return {'state': 0, 'dom': {key: ['0'] for key in arg}}
                if 'const lines=' in expression:
                    return [marker, '预订住宿 2026-10-20 至 2026-10-22']
                if "querySelectorAll('input,textarea,select')" in expression:
                    return []
                return True

        def new_page():
            page = Page(True)
            page.on('response', session._observe)
            return page

        session.context = SimpleNamespace(new_page=new_page, cookies=lambda *_: [])
        session._verify_oa(deadline)

    monkeypatch.setattr(sessions.TravelReadSession, '_start_engine', start_browser)
    workflow = json.loads((Path(__file__).parent.parent / 'collection' / 'fixtures' / 'travel_empty_placeholders.json').read_text())
    for field in workflow['workflowMainTableInfo']['requestRecords'][0]['workflowRequestTableFields']:
        if field['fieldName'] == 'fjsl':
            field['fieldValue'] = '<a href="/sn_slv_bxbz.jsp">0</a>'
    api = FakeOA()
    api.rows = [{**api.rows[0], 'request_id': number} for number in ('12', '13')]
    api.request_context = lambda request_id: ({**deepcopy(workflow), 'requestId': request_id}, [])

    def create(engine):
        policy = replace(POLICY, travel_read=TravelReadPolicy(engine, 'api')) if engine == 'moli' else POLICY
        return Approval(tmp_path / 'runtime', api=api, writer=api, configs=[TRAVEL_CONFIG], source_policy=policy)

    return create, api, failures, fault, orders_read


@pytest.mark.parametrize('engine,timed_out', [
    ('playwright', set()), ('playwright', {'hotel-1'}), ('playwright', {'hotel-1', 'hotel-2'}),
    ('moli', set()),
])
def test_collection_preserves_each_hotels_read_and_stay_date_semantics(collected_hotels, engine, timed_out):
    create, api, failures, _, orders_read = collected_hotels
    failures.update(timed_out)
    app = create(engine)
    started = app.pending(batch_size=2)
    first, second = (task['id'] for task in started['tasks'])
    try:
        result = app.collect(started['run_id'], expected_task_id=first)
        material = json.loads(Path(result['material_file']).read_text())
        assert result['status'] == 'prepared'
        assert material['attachment_presence']['status'] == 'confirmed_empty'
        facts = material['authority_facts']
        if engine == 'moli':
            amount, = [fact for fact in facts if fact['name'] == '财务审批补助金额']
            assert amount['source_endpoint'] == '/fcecf/fcc/pcapi/COMM_B2G_LoadBxdBaseInfoDetails'
            assert amount['source_json_path'] == 'result.fkBxdKzb.cwspbzje'
        details = [json.loads(fact['value']) for fact in facts if fact['name'] == '商旅住宿订单详情']
        assert [detail['order_no'] for detail in details] == ['hotel-1', 'hotel-2']
        for detail in details:
            assert detail['diagnostics']['elapsed_ms'] >= 0
            if detail['order_no'] in timed_out:
                assert detail['read_status'] == 'failed' and detail['reason'] == 'hotel_order_detail_timeout'
                assert detail['diagnostics']['exception_type'] == 'TimeoutError'
                assert 'check_in' not in detail and 'check_out' not in detail
            else:
                assert detail['read_status'] == 'read' and detail['date_semantics'] == 'reserved'
                assert (detail['check_in'], detail['check_out']) == ('2026-10-20', '2026-10-22')
        assert orders_read == ['hotel-1', 'hotel-2']
        assert 'synthetic private detail' not in json.dumps(material)
        assert not material.get('travel_read_exception') and api.posts == []
        app.fail(started['run_id'], first, 'synthetic test stops after verifying collection')
        failures.clear()
        normal = app.collect(started['run_id'], expected_task_id=second)
        assert normal['status'] == 'prepared'
        assert orders_read == ['hotel-1', 'hotel-2'] * 2
    finally:
        app.store.connection.close()


@pytest.mark.parametrize('timed_out', [{'hotel-1'}, {'hotel-1', 'hotel-2'}])
def test_compatible_collection_rejects_undelivered_hotel_responses(collected_hotels, timed_out):
    create, api, failures, _, orders_read = collected_hotels
    failures.update(timed_out)
    app = create('moli')
    started = app.pending(batch_size=1)
    task_id = started['tasks'][0]['id']
    try:
        with pytest.raises(RuntimeError, match='^travel_attachment_discovery_failed$'):
            app.collect(started['run_id'], expected_task_id=task_id)
        result = app.inspect(task_id)
        assert result['task']['status'] == 'failed' and not result['task']['material_file']
        diagnostics = result['collection_failure']['diagnostics']
        assert diagnostics[0]['reason'] == 'travel_delivery_incomplete'
        assert sum(row['reason'] == 'client_write_error' for row in diagnostics) == 2 * len(timed_out)
        reads = [row for row in diagnostics if row['stage'] == 'hotel_detail']
        assert [(row['browser'], row['source_path'], row['read_status'], row['reason']) for row in reads] == [
            (engine, f'hotel_details[{index}]',
             'failed' if order in timed_out else 'read',
             'hotel_order_detail_timeout' if order in timed_out else 'hotel_order_detail_read')
            for engine in ('moli', 'chromium')
            for index, order in enumerate(('hotel-1', 'hotel-2'))
        ]
        assert all(row['elapsed_ms'] >= 0 for row in reads)
        assert orders_read == ['hotel-1', 'hotel-2'] * 2
        assert '合成市' not in json.dumps(diagnostics, ensure_ascii=False)
        assert 'synthetic private detail' not in json.dumps(diagnostics)
        assert api.posts == []
    finally:
        app.store.connection.close()


@pytest.mark.parametrize('engine,late_failure', [
    ('playwright', 'scan'), ('playwright', 'deadline'), ('moli', 'deadline'),
])
def test_collection_keeps_completed_hotel_reads_after_a_later_failure(
        collected_hotels, browser_transport, tmp_path, engine, late_failure):
    from apps.oa.runtime import collection_budget

    create, api, _, fault, orders_read = collected_hotels
    phase_file = tmp_path / 'phase.json'

    def check_partial_receipt():
        checkpoint = phase_file.with_suffix('.read.json').read_text()
        progress = json.loads(checkpoint)['read_results'][-1]
        assert progress['scope'] == 'hotel_order'
        assert progress['diagnostics'][0]['source_path'] == 'hotel_details[0]'
        assert progress['status'] == 'read'
        assert '合成市' not in checkpoint and 'hotel-1' not in checkpoint

    if late_failure == 'deadline':
        fault['deadline_order'] = 'hotel-2'
        fault['before_deadline'] = check_partial_receipt
    else:
        browser_transport.outcomes[True]['scan_failure'] = True
    browser_transport.outcomes[False] = browser_transport.outcomes[True]
    app = create(engine)
    started = app.pending(batch_size=1)
    task_id = started['tasks'][0]['id']
    try:
        with collection_budget.tracking(phase_file), pytest.raises(
                RuntimeError, match='^travel_attachment_discovery_failed$'):
            app.collect(started['run_id'], expected_task_id=task_id)
        result = app.inspect(task_id)
        assert result['task']['status'] == 'failed' and not result['task']['material_file']
        diagnostics = result['collection_failure']['diagnostics']
        completed = [row for row in diagnostics if row['stage'] == 'hotel_detail']
        indexes = [0] if late_failure == 'deadline' else [0, 1]
        browsers = ('headless', 'visible') if engine == 'playwright' else ('moli', 'chromium')
        candidate_count = 2 if late_failure == 'scan' else 1
        assert [(row['browser'], row['source_path'], row['read_status']) for row in completed] == [
            (browser, f'hotel_details[{index}]', 'read')
            for browser in browsers for _ in range(candidate_count) for index in indexes
        ]
        assert all(row['elapsed_ms'] >= 0 for row in completed)
        assert orders_read == ['hotel-1', 'hotel-2'] * (2 * candidate_count)
        assert '合成市' not in json.dumps(diagnostics, ensure_ascii=False)
        assert 'synthetic private' not in json.dumps(diagnostics)
        assert api.posts == []
    finally:
        app.store.connection.close()


def test_compatible_hotel_diagnostics_preserve_the_late_identity_guard(collected_hotels):
    create, api, failures, session_fault, orders_read = collected_hotels
    failures.add('hotel-1')
    session_fault['after_orders'] = 'identity'
    app = create('moli')
    started = app.pending(batch_size=1)
    task_id = started['tasks'][0]['id']
    try:
        with pytest.raises(RuntimeError, match='travel_attachment_discovery_failed'):
            app.collect(started['run_id'], expected_task_id=task_id, travel_read_exception={
                'request_id': '12', 'authorization_note': 'Synthetic missing-account rule',
                'allowed_reasons': ['travel_sso_account_missing'],
            })
        inspection = app.inspect(task_id)
        assert inspection['task']['status'] == 'failed' and not inspection['task']['material_file']
        diagnostics = inspection['collection_failure']['diagnostics']
        assert diagnostics[0]['reason'] == 'travel_session_identity_mismatch'
        assert [(detail['browser'], detail['source_path'], detail['reason'])
                for detail in diagnostics if detail['stage'] == 'hotel_detail'] == [
            ('moli', 'hotel_details[0]', 'hotel_order_detail_timeout'),
            ('moli', 'hotel_details[1]', 'hotel_order_detail_read'),
        ]
        assert orders_read == ['hotel-1', 'hotel-2']
        assert 'synthetic private detail' not in json.dumps(diagnostics)
        assert '合成市' not in json.dumps(diagnostics, ensure_ascii=False)
        assert api.posts == []
    finally:
        app.store.connection.close()
