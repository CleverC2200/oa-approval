from __future__ import annotations

import dataclasses
import decimal
import json
import re
import time
from playwright.sync_api import Frame

from apps.oa.approvals.sources.travel_browser import _CandidateSession, _browser_roots
from apps.oa.approvals.sources import travel_hotel

BASE_PATH = "/fcecf/fcc/pcapi/COMM_B2G_LoadBxdBaseInfoDetails"
EXP_PATH = "/fcecf/fcc/pcapi/COMM_B2G_LoadBxmxList"
SUMMARY_PATH = "/fcecf/fcc/pcapi/COMM_B2G_LoadBxdJefo"
HOTEL_PATHS = {
    "/hotel/fcc/hotel/order/b2ghotelzcd/getOrderDetail",
    "/fcecf/fcc/pcapi/COMM_B2G_bldDetail",
}
SUMMARY_FIELDS = ["hjdkse", "hjbhsje", "fzpseTotal", "zce", "cwxtsjfkje", "fpyqe"]


class ReadFailure(ValueError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


VISIBLE = "e=>{\n if(!e||!e.isConnected)return false;\n for(let n=e;n;n=n.parentElement){let s=getComputedStyle(n);if(n.hidden||n.getAttribute('aria-hidden')==='true'||s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||s.opacity==='0')return false;}\n return true;\n}"


class VisibleReader:
    def __init__(self):
        self.samples = []

    def controls(self, root):
        return root.evaluate(
            "()=>Array.from(document.querySelectorAll('input,textarea,select')).filter(e=>{\n   if(['hidden','password','file','button','submit','reset'].includes((e.type||'').toLowerCase()))return false;\n   if(['checkbox','radio'].includes(e.type)&&!e.checked)return false;\n   return ("
            + VISIBLE
            + ")(e);\n  }).map(e=>({label:(e.labels?.[0]?.textContent||e.getAttribute('aria-label')||e.name||e.id||e.tagName).trim(),value:e.tagName==='SELECT'?Array.from(e.selectedOptions).map(x=>x.label||x.textContent).join(' / '):e.value})).filter(x=>String(x.value||'').trim())"
        )

    def read(self, root):
        started = time.perf_counter()
        if isinstance(root, Frame):
            owner = root.frame_element()
            if not owner.evaluate(VISIBLE):
                return ""
            parked = owner.evaluate(
                "e=>e.id==='wdatepicker_container'&&Array.from((function*(n){for(;n;n=n.parentElement)yield n;})(e)).some(n=>{let s=getComputedStyle(n);return ['absolute','fixed'].includes(s.position)&&parseFloat(s.left)<0&&parseFloat(s.top)<0;})"
            )
            if parked:
                return ""
        lines = root.evaluate(
            "()=>{\n   const lines=[];const cache=new Map();\n   function shown(e){if(cache.has(e))return cache.get(e);const s=getComputedStyle(e);const value=!e.hidden&&e.getAttribute('aria-hidden')!=='true'&&s.display!=='none'&&s.visibility!=='hidden'&&s.visibility!=='collapse'&&s.opacity!=='0';cache.set(e,value);return value;}\n   function walk(n){\n    if(n.nodeType===3){let t=n.textContent.trim();if(t)lines.push(t);return;}\n    if(n.nodeType!==1||!shown(n)||['SCRIPT','STYLE','TEMPLATE','NOSCRIPT','INPUT','TEXTAREA','SELECT'].includes(n.tagName.toUpperCase()))return;\n    for(const child of n.childNodes)walk(child);\n   }\n   if(document.body)walk(document.body);\n   return lines;\n  }"
        )
        controls = self.controls(root)
        lines.extend((c["label"] + "：" + c["value"] for c in controls))
        text = "\n".join(lines)
        self.samples.append(
            {
                "ms": round((time.perf_counter() - started) * 1000, 3),
                "characters": len(text),
                "reader": "visible_dom_text_and_controls",
            }
        )
        return text


def source_data(payloads, marker):
    def latest(path):
        matches = [p for p in payloads if p["path"] == path]
        if not matches:
            raise ReadFailure("travel_source_pending")
        reply = matches[-1]
        if reply["status"] != 200 or reply["payload"].get("success") is not True:
            raise ReadFailure("travel_source_failed")
        result = reply["payload"].get("result")
        if not isinstance(result, dict):
            raise ReadFailure("travel_source_invalid")
        return result

    base = latest(BASE_PATH)
    main = base.get("bxdzb")
    rows = latest(EXP_PATH).get("bxmxList")
    if (
        not isinstance(main, dict)
        or not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise ReadFailure("travel_source_invalid")
    if not marker or main.get("bxdh") != marker or not main.get("bxpch"):
        raise ReadFailure("travel_source_binding_mismatch")
    seen = set()
    facts = []
    for index, row in enumerate(rows):
        rid = str(row.get("id") or "")
        if not rid or rid in seen or row.get("bxpch") != main["bxpch"]:
            raise ReadFailure("travel_source_row_binding_mismatch")
        seen.add(rid)
        for key, label in [
            ("fysm", "费用说明"),
            ("fplxmc", "发票类型"),
            ("se", "源税额 se"),
            ("xcrqs", "完整开始时间"),
            ("xcrqz", "完整结束时间"),
        ]:
            if row.get(key) not in (None, ""):
                facts.append(
                    {
                        "name": label,
                        "value": str(row[key]),
                        "subject_ref": "travel_expense:" + rid,
                        "source_path": EXP_PATH,
                        "source_json_path": f"result.bxmxList[{index}].{key}",
                        "field": key,
                        "display_claim": "只读源接口补充，不宣称界面已展示",
                    }
                )
    amount = (base.get("fkBxdKzb") or {}).get("cwspbzje")
    if amount not in (None, ""):
        facts.append(
            {
                "name": "财务审批补助金额",
                "value": str(amount),
                "subject_ref": "travel_report:" + marker,
                "field": "cwspbzje",
                "source_path": BASE_PATH,
                "source_json_path": "result.fkBxdKzb.cwspbzje",
                "display_claim": "只读源接口补充，不替代界面校验",
            }
        )
    return base, rows, facts


def number(value):
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        return None
    result = decimal.Decimal(text.replace(",", ""))
    return result if result.is_finite() else None


class BodyLocator:
    def __init__(self, locator, page):
        self.locator, self.page = locator, page

    def inner_text(self, **kwargs):
        return self.page.owner.reader.read(self.page.raw)

    def __getattr__(self, name):
        return getattr(self.locator, name)


class AttachmentTab:
    def __init__(self, page, locator):
        self.page, self.locator = page, locator

    def count(self):
        return self.locator.count()

    def nth(self, index):
        return AttachmentTab(self.page, self.locator.nth(index))

    def is_visible(self):
        return self.page.raw.locator(
            '.jsTabBox .jsTab[data-page="fjxx-page"]'
        ).count() == 1 and self.locator.evaluate(VISIBLE)

    def click(self, *, timeout):
        if not self.is_visible():
            raise ReadFailure("travel_attachment_tab_ambiguous", retryable=True)
        self.locator.evaluate("e=>e.click()")
        self.page.raw.wait_for_function(
            """()=>{
            const panels=document.querySelectorAll('.jsPageBox[data-page="fjxx-page"]');
            return panels.length===1&&("""
            + VISIBLE
            + """)(panels[0])&&!!panels[0].querySelector('.bxd-fjxxlist-table tbody .no_data,.bxd-fjxxlist-table tbody .fjxx-tr');
        }""",
            timeout=timeout,
        )


class ReadPage:
    def __init__(self, page, owner):
        self.raw, self.owner = page, owner

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def locator(self, selector, *args, **kwargs):
        locator = self.raw.locator(selector, *args, **kwargs)
        return BodyLocator(locator, self) if selector == "body" else locator

    def get_by_text(self, text, **kwargs):
        if text == "附件信息" and kwargs.get("exact"):
            return AttachmentTab(
                self, self.raw.locator('.jsTabBox .jsTab[data-page="fjxx-page"]')
            )
        return self.raw.get_by_text(text, **kwargs)

    def goto(self, url, **kwargs):
        response = self.raw.goto(url, **kwargs)
        if response and response.status >= 400:
            raise ReadFailure(
                "travel_login_required"
                if response.status in {401, 403}
                else "travel_source_failed"
            )
        previous = None
        stable = 0
        while time.monotonic() < self.owner.deadline:
            self.owner.session.check_transport()
            try:
                base, _, _ = source_data(self.owner.payloads, self.owner.marker)
            except ReadFailure as error:
                if error.code != "travel_source_pending":
                    raise
            else:
                main = base["bxdzb"]
                bound = self.raw.evaluate(
                    """expected=>{
                    const p=document.querySelector('.jsPageBox[data-page="bxdxx-page"]');
                    return p&&getComputedStyle(p).display!=='none'&&expected.title&&p.querySelector('#bxbt')?.value===expected.title&&(!expected.person||p.querySelector('#bxrxm')?.value===expected.person);
                }""",
                    {"title": main.get("bxbt"), "person": main.get("bxrxm")},
                )
                if bound:
                    text = self.owner.reader.read(self.raw)
                    stable = stable + 1 if text == previous else 0
                    previous = text
                    if stable >= 2:
                        self.verify_money("navigation_ready")
                        return response
            self.raw.wait_for_timeout(150)
        source_data(self.owner.payloads, self.owner.marker)
        raise ReadFailure("travel_dom_not_ready", retryable=True)

    def verify_money(self, stage):
        until = min(self.owner.deadline, time.monotonic() + 8)
        stable = 0
        previous = None
        while time.monotonic() < until:
            self.owner.session.check_transport()
            base, _, _ = source_data(self.owner.payloads, self.owner.marker)
            replies = [p for p in self.owner.payloads if p["path"] == SUMMARY_PATH]
            if replies:
                latest = replies[-1]
                if (
                    latest["status"] != 200
                    or latest["payload"].get("success") is not True
                ):
                    raise ReadFailure("travel_source_failed")
                expected = latest["payload"].get("result")
                if not isinstance(expected, dict):
                    raise ReadFailure("travel_source_invalid")
                values = self.raw.evaluate(
                    """keys=>({
                    state:window.bxdAppData?.fkBxdKzb?.cwspbzje,
                    dom:Object.fromEntries(keys.map(k=>[k,Array.from(document.querySelectorAll('#costTotal .sub-item.'+k+' .right')).map(e=>e.textContent.trim())]))
                })""",
                    ["cwspbzje", *SUMMARY_FIELDS],
                )
                source_amount = (base.get("fkBxdKzb") or {}).get("cwspbzje")
                matches = (
                    number(source_amount) is not None
                    and number(source_amount)
                    == number(values["state"])
                    == number(expected.get("cwspbzje"))
                    and all(
                        number(expected.get(k)) is not None
                        and len(values["dom"][k]) == 1
                        and number(values["dom"][k][0]) == number(expected[k])
                        for k in ["cwspbzje", *SUMMARY_FIELDS]
                    )
                )
                fingerprint = json.dumps(values, sort_keys=True)
                stable = (
                    stable + 1
                    if matches and fingerprint == previous
                    else 1
                    if matches
                    else 0
                )
                previous = fingerprint
                if stable >= 3:
                    self.owner.money_checks.append(
                        {"stage": stage, "passed": True, "samples_stable": stable}
                    )
                    return
            self.raw.wait_for_timeout(150)
        raise ReadFailure("travel_summary_not_ready", retryable=True)


class BrowserResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def json(self):
        return self.body


class BrowserRequest:
    def __init__(self, owner):
        self.owner = owner

    def post(self, url, *, data=None, headers=None, timeout=5000, max_redirects=0):
        if not self.owner.active_page:
            raise ReadFailure("travel_page_missing")
        result = self.owner.active_page.evaluate(
            """async p=>{
            const a=new AbortController();const timer=setTimeout(()=>a.abort(),p.timeout);
            try{const headers={};for(const [k,v] of Object.entries(p.headers))if(k.toLowerCase()!=='referer')headers[k]=v;
                const body=p.data===null?undefined:new URLSearchParams(p.data).toString();
                if(body!==undefined)headers['Content-Type']='application/x-www-form-urlencoded';
                const r=await fetch(p.url,{method:'POST',headers,body,credentials:'include',redirect:'manual',signal:a.signal});
                const text=await r.text();let value=null;try{value=JSON.parse(text)}catch{}
                return {status:r.type==='opaqueredirect'?302:r.status,body:value};
            }finally{clearTimeout(timer)}
        }""",
            {"url": url, "data": data, "headers": headers or {}, "timeout": timeout},
        )
        return BrowserResponse(result["status"], result["body"])


class ReadContext:
    def __init__(self, raw, session, marker, deadline):
        self.raw, self.session, self.marker, self.deadline = (
            raw,
            session,
            marker,
            deadline,
        )
        self.reader = VisibleReader()
        self.payloads = session.payloads
        self.money_checks = []
        self.active_page = None
        self.request = BrowserRequest(self)

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def new_page(self):
        self.active_page = self.raw.new_page()
        return ReadPage(self.active_page, self)


def verify_trip_identity(page, main):
    user = page.evaluate(
        """()=>{let u=window.user||window.VEGLOBAL?.FCCUSER;return u?{id:String(u.ygid||''),account:String(u.gh||''),tenant:String(u.qybh||'')}:null}"""
    )
    expected = {
        "id": main.get("bxrid"),
        "account": main.get("tdrgh"),
        "tenant": main.get("qybh"),
    }
    if not user or any(
        value in (None, "") or user.get(key) != str(value)
        for key, value in expected.items()
    ):
        raise ReadFailure("travel_session_identity_mismatch")


class CompatibleCandidate(_CandidateSession):
    def read(self):
        try:
            self.page.goto(
                self.url,
                wait_until="domcontentloaded",
                timeout=max(1000, int(self.budget * 1000)),
            )
            base, _, _ = source_data(self.context.payloads, self.context.marker)
            verify_trip_identity(self.page.raw, base["bxdzb"])
            initial = self.page.locator("body").inner_text()
            details = travel_hotel.read_details(
                self.context.request,
                self.capture.hotel_orders,
                origin=self.policy.travel_origin,
                timeout_sec=max(0.1, self.deadline - time.monotonic()),
            )
            if any(d.get("read_status") != "read" for d in details):
                raise ReadFailure("travel_hotel_source_failed")
            self.page.verify_money("before_attachment_scan")
            _, _, facts = source_data(self.context.payloads, self.context.marker)
            inventory = self.capture.inventory
            if any(
                r.get("reason") not in {"downloadable", "empty_placeholder"}
                for r in inventory
            ):
                raise ReadFailure("travel_source_inventory_invalid")
            files = {r["file_ref"] for r in inventory if r.get("file_ref")}
            if (
                self.expected_resource_count is not None
                and self.expected_resource_count != len(files)
            ):
                raise ReadFailure("travel_source_inventory_count_mismatch")
            self.expected_resource_count = len(files)
            self.context.session.transport.policy.files = frozenset(files)
            self.stage = "attachment_scan"
            snapshot = self._read_attachment_snapshot(
                timeout_sec=max(0.1, self.deadline - time.monotonic())
            )
            self.page.verify_money("after_attachment_scan")
            result = super()._observation(initial, details, snapshot)
            if not result.assess(
                expected_marker=self.context.marker, expected_resource_count=len(files)
            ).attempt_complete:
                raise ReadFailure("travel_dom_scan_incomplete", retryable=True)
            base, _, _ = source_data(self.context.payloads, self.context.marker)
            verify_trip_identity(self.page.raw, base["bxdzb"])
            targets = tuple(
                {k: v for k, v in r.items() if k != "_ephemeral_cookie_header"}
                for r in result.download_targets
            )
            self.context.session.page = self.page.raw
            return dataclasses.replace(
                result, download_targets=targets, source_facts=tuple(facts)
            )
        except ReadFailure:
            raise
        except Exception as error:
            self.context.session.check_transport()
            raise ReadFailure("travel_engine_read_failed", retryable=True) from error

    def _browser_snapshot(self, page, *, fallback_url):
        snapshot = super()._browser_snapshot(page, fallback_url=fallback_url)
        texts = []
        for root in _browser_roots(page):
            raw = getattr(root, "raw", root)
            text = self.context.reader.read(raw).strip()
            if text and text not in texts:
                texts.append(text)
        snapshot["text"] = "\n".join(texts)
        return snapshot
