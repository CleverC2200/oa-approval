from __future__ import annotations

import signal
from types import SimpleNamespace

import pytest

from apps.oa_approval import attachments, travel_orders


@pytest.mark.parametrize('address,identity', [('合成路1号', 'hotel-1'), ('', 'hotel-1'), ('合成路1号', 'other')])
def test_hotel_details_preserve_complete_result_and_identity(address, identity):
    result = {'ddbh': identity, 'jdmc': '合成酒店', 'jddz': address, 'jdcszwmc': '杭州市',
              'xzqmc': '西湖区', 'ydrzrq': '2026-10-20', 'ydldrq': '2026-10-21', 'ddztmc': '已完成'}
    context = SimpleNamespace(request=SimpleNamespace(post=lambda *_a, **_k: SimpleNamespace(
        json=lambda: {'result': result})))
    details = travel_orders._read_hotel_order_details(
        context, [{'order_no': 'hotel-1', 'expense_description': '合成住宿'}], timeout_sec=1)
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

    transport = SimpleNamespace(launches=[], browsers=[], outcomes={}, exits=[])

    class Page:
        frames = []
        main_frame = None

        def __init__(self, headless):
            self.outcome = transport.outcomes.get(headless, {})
            self.url = ''

        def on(self, *_args):
            pass

        def goto(self, url, **_kwargs):
            self.url = url
            failure = self.outcome.get('failure')
            if failure == 'deadline':
                signal.raise_signal(signal.SIGALRM)
            if failure == 'read':
                raise RuntimeError('synthetic navigation failure')

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

    class Browser:
        def __init__(self, headless):
            self.page = Page(headless)
            self.disposed = False
            self.close_timers = []

        def new_context(self, **_kwargs):
            return SimpleNamespace(new_page=lambda: self.page, cookies=lambda *_a: [])

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
    monkeypatch.setattr(travel_orders, '_ensure_cdp_available', lambda: True)
    monkeypatch.setattr(travel_orders, 'TRIP_BASE_URL', 'https://trip.example.test')
    monkeypatch.setattr(attachments, 'rpa_storage_state', lambda **_k: {'cookies': [], 'origins': []})
    monkeypatch.setattr(attachments, 'browser_visible_fallback_enabled', lambda: True)
    return transport


def collect_travel(count='0'):
    return travel_orders.collect_evidence(
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
