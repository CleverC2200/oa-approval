from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any
import urllib.parse


HOTEL_ORDER_DETAIL_PATH = "/hotel/fcc/hotel/order/b2ghotelzcd/getOrderDetail"
OFFLINE_EXPENSE_DETAIL_PATH = "/fcecf/fcc/pcapi/COMM_B2G_bldDetail"


@dataclass(frozen=True, slots=True)
class _DetailRequest:
    url: str
    referer: str
    data: dict[str, str] | None
    identities: dict[str, str]
    require_success: bool


class _OnlineOrder:
    source = "online_order"

    def prepare(self, origin: str, order: dict, order_no: str) -> _DetailRequest:
        query = urllib.parse.urlencode({"ddbh": order_no})
        return _DetailRequest(
            f"{origin}{HOTEL_ORDER_DETAIL_PATH}?{query}",
            f"{origin}/fcc/hotel/order/zcd/hotelOrderDetail.html?{query}",
            None,
            {"ddbh": order_no},
            False,
        )

    def project(self, result: dict) -> dict:
        return {
            "hotel_name": str(result.get("jdmc") or "").strip(),
            "hotel_address": str(result.get("jddz") or "").strip(),
            "city": str(result.get("jdcszwmc") or "").strip(),
            "district": str(result.get("xzqmc") or "").strip(),
            "check_in": str(result.get("ydrzrq") or "").strip(),
            "check_out": str(result.get("ydldrq") or "").strip(),
            "date_semantics": "reserved",
        }


class _OfflineExpense:
    source = "offline_expense"

    def prepare(self, origin: str, order: dict, order_no: str) -> _DetailRequest | None:
        if not all(str(order.get(key) or "").strip() for key in ("id", "bxpch")):
            return None
        return _DetailRequest(
            f"{origin}{OFFLINE_EXPENSE_DETAIL_PATH}",
            f"{origin}/fcc/fcecf/bxdHome/index.html",
            {"id": order["id"], "ywdh": order_no, "bxpch": order["bxpch"]},
            {
                "id": order.get("id"),
                "ywdh": order_no,
                "bxpch": order.get("bxpch"),
                "sjly": order.get("sjly"),
                "cpbh": "0300",
                "ddlx": "03001",
            },
            True,
        )

    def project(self, result: dict) -> dict:
        extension = result.get("fkFymxKzbBean")
        return {
            "hotel_name": str(result.get("jdmc") or "").strip(),
            "hotel_address": str(
                (extension if isinstance(extension, dict) else {}).get("jddz") or ""
            ).strip(),
            "city": str(result.get("ddcs") or "").strip(),
            "district": str(result.get("ddcsqx") or "").strip(),
            "check_in": str(result.get("xcrqs") or "").strip(),
            "check_out": str(result.get("xcrqz") or "").strip(),
            "date_semantics": "declared",
            "location_semantics": "declared",
        }


def read_details(
    request, orders: list[dict], *, origin: str, timeout_sec: float
) -> list[dict]:
    details = []
    deadline = time.monotonic() + max(0.5, float(timeout_sec))
    for order in orders:
        remaining = deadline - time.monotonic()
        order_no = str(order.get("order_no") or "").strip()
        if not order_no:
            continue
        if remaining <= 0:
            details.append(
                {
                    **order,
                    "read_status": "failed",
                    "reason": "hotel_order_detail_timeout",
                    "diagnostics": {"stage": "hotel_detail"},
                }
            )
            continue
        protocol = (
            _OfflineExpense()
            if str(order.get("sjly") or "").strip() not in {"", "2"}
            else _OnlineOrder()
        )
        detail = {**order, "detail_source": protocol.source}
        diagnostics: dict[str, Any] = {"stage": "hotel_detail"}
        prepared = protocol.prepare(origin, order, order_no)
        if prepared is None:
            details.append(
                {
                    **detail,
                    "read_status": "failed",
                    "reason": "hotel_expense_identity_missing",
                    "diagnostics": diagnostics,
                }
            )
            continue
        reason, payload = "", None
        try:
            response = request.post(
                prepared.url,
                **({"data": prepared.data} if prepared.data is not None else {}),
                headers={
                    "referer": prepared.referer,
                    "x-requested-with": "XMLHttpRequest",
                },
                timeout=max(500, min(int(remaining * 1000), 5000)),
                max_redirects=0,
            )
            status = getattr(response, "status", 200)
            diagnostics["http_status"] = status
            if status in {401, 403}:
                reason = "hotel_order_authentication_failed"
            elif 300 <= status < 400:
                reason = "hotel_order_redirected"
            elif not 200 <= status < 300:
                reason = "hotel_order_http_failed"
            else:
                reason = "hotel_order_response_invalid"
                payload = response.json()
                reason = ""
        except Exception as error:
            diagnostics["exception_type"] = type(error).__name__
            reason = (
                "hotel_order_detail_timeout"
                if type(error).__name__ == "TimeoutError"
                else reason or "hotel_order_detail_read_failed"
            )
        result = payload.get("result") if isinstance(payload, dict) else None
        diagnostics["result_type"] = type(result).__name__
        if isinstance(payload, dict):
            if isinstance(payload.get("success"), bool):
                diagnostics["api_success"] = payload["success"]
            code = payload.get("code")
            if isinstance(code, int) and not isinstance(code, bool):
                diagnostics["api_code"] = code
        identities = prepared.identities
        if not reason:
            if not isinstance(payload, dict):
                reason = "hotel_order_response_invalid"
            elif payload.get("success") is False:
                reason = "hotel_order_api_failed"
            elif (
                "success" in payload and not isinstance(payload["success"], bool)
            ) or (prepared.require_success and payload.get("success") is not True):
                reason = "hotel_order_response_invalid"
            elif result is None:
                reason = "hotel_order_result_missing"
            elif not isinstance(result, dict):
                reason = "hotel_order_result_invalid"
            elif not all(str(result.get(key) or "").strip() for key in identities):
                reason = "hotel_order_identity_missing"
            elif any(
                str(result[key]).strip() != value for key, value in identities.items()
            ):
                reason = "hotel_order_identity_mismatch"
        if reason:
            if isinstance(result, dict):
                diagnostics["identity_present"] = all(
                    bool(result.get(key)) for key in identities
                )
                diagnostics["identity_matches"] = all(
                    str(result.get(key) or "").strip() == value
                    for key, value in identities.items()
                )
            details.append(
                {
                    **detail,
                    "read_status": "failed",
                    "reason": reason,
                    "diagnostics": diagnostics,
                }
            )
            continue
        details.append(
            {
                **detail,
                "read_status": "read",
                "order_detail": result,
                **protocol.project(result),
            }
        )
    return details


def orders_from_expenses(payload: Any) -> list[dict[str, str]]:
    orders: list[dict[str, str]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            order_no = str(value.get("ywdh") or "").strip()
            expense_description = str(value.get("fysm") or "").strip()
            if (
                str(value.get("cpbh") or "").strip() == "0300"
                and str(value.get("ddlx") or "").strip() == "03001"
                and order_no
            ):
                order = {
                    "order_no": order_no,
                    "expense_description": expense_description,
                    **{
                        key: str(value.get(key) or "").strip()
                        for key in ("sjly", "id", "bxpch")
                    },
                }
                if order not in orders:
                    orders.append(order)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return orders


def _detail_subject_ref(
    fields: list[dict[str, Any]],
    expense_description: str,
) -> str:
    matches: set[str] = set()
    for field in fields:
        if (
            str(field.get("name") or "").strip() != "费用说明"
            or str(field.get("value") or "").strip() != expense_description
        ):
            continue
        path = str(field.get("path") or "").strip()
        subject_ref = path.rsplit(".field[", 1)[0] if ".field[" in path else ""
        if subject_ref:
            matches.add(subject_ref)
    return next(iter(matches)) if len(matches) == 1 else ""


def fields_for_details(
    fields: list[dict[str, Any]],
    details: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for detail in details:
        if not isinstance(detail, dict):
            continue
        address = str(detail.get("hotel_address") or "").strip()
        expense_description = str(detail.get("expense_description") or "").strip()
        subject_ref = _detail_subject_ref(fields, expense_description)
        if not subject_ref:
            order_no = str(detail.get("order_no") or "").strip()
            if not order_no:
                continue
            subject_ref = f"travel_order:{order_no}"
        body = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        identity = (subject_ref, body)
        if identity in seen:
            continue
        seen.add(identity)
        facts.append(
            {
                "name": "商旅住宿订单详情",
                "value": body,
                "raw_value": body,
                "table": "travel_order",
                "row": len(facts),
                "subject_ref": subject_ref,
            }
        )
        if not address or detail.get("location_semantics") == "declared":
            continue
        facts.append(
            {
                "name": "酒店地址",
                "value": address,
                "raw_value": address,
                "table": "travel_order",
                "row": len(facts),
                "subject_ref": subject_ref,
            }
        )
    return facts
