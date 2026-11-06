from __future__ import annotations

import json
import re
import time
import urllib.parse


TRAIN_DETAIL_PATH = "/train/fcc/train/normal/detail"


def read_invoices(request, expenses, *, marker, origin, timeout_sec):
    if expenses is None:
        return [{"read_status": "failed", "reason": "travel_order_expenses_unavailable", "invoices": []}]
    records = []
    deadline = time.monotonic() + timeout_sec
    orders = {}
    for expense in expenses:
        if (str(expense.get("cpbh")), str(expense.get("ddlx")), str(expense.get("sjly"))) != ("0600", "06001", "2"):
            continue
        record = {
            "order_no": str(expense.get("ywdh") or ""),
            "expense_id": str(expense.get("id") or ""),
            "expense_dbid": str(expense.get("dbid") or ""),
            "employee_id": str(expense.get("ygid") or ""),
            "invoices": [],
        }
        records.append(record)
        try:
            if expense.get("bxdh") != marker or not all(record[key] for key in ("order_no", "expense_id", "expense_dbid", "employee_id")):
                raise ValueError("travel_order_expense_binding_invalid")
            order_no = record["order_no"]
            if order_no not in orders:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("travel_order_invoice_timeout")
                response = request.post(
                    origin + TRAIN_DETAIL_PATH,
                    data=json.dumps({"ddbh": order_no, "czlx": ""}),
                    headers={"Content-Type": "application/json", "x-requested-with": "XMLHttpRequest"},
                    timeout=max(1, min(int(remaining * 1000), 5000)),
                    max_redirects=0,
                )
                if response.status != 200:
                    raise ValueError("travel_order_invoice_http_failed")
                payload = response.json()
                if not isinstance(payload, dict) or str(payload.get("status")) != "200" or not isinstance(payload.get("result"), dict):
                    raise ValueError("travel_order_invoice_response_invalid")
                result = payload["result"]
                if result.get("ddbh") != order_no:
                    raise ValueError("travel_order_invoice_order_mismatch")
                orders[order_no] = result
            result = orders[order_no]
            passengers, invoices = result.get("ckxxVOList"), result.get("invoiceList")
            if (invoices is None and result.get("sfkp") == "0"
                    and isinstance(passengers, list) and passengers
                    and all(isinstance(item, dict) and item.get("kpzt") == "0" for item in passengers)):
                invoices = []
                record["invoice_status"] = "not_submitted"
            if not isinstance(passengers, list) or not isinstance(invoices, list) or any(not isinstance(item, dict) for item in passengers + invoices):
                record["diagnostics"] = [
                    {"stage": "travel_order_invoice", "source_path": key,
                     "reason": "invalid_records", "result_type": type(result.get(key)).__name__}
                    for key in ("ckxxVOList", "invoiceList")
                    if not isinstance(result.get(key), list)
                    or any(not isinstance(item, dict) for item in result[key])]
                raise ValueError("travel_order_invoice_records_invalid")
            own_passengers = [p for p in passengers if p.get("cxrYgid") == record["employee_id"]]
            if not own_passengers:
                raise ValueError("travel_order_invoice_passenger_missing")
            for invoice in invoices:
                matches = [p for p in passengers if p.get("id") and p["id"] == invoice.get("mxid")]
                if len(matches) != 1 or not matches[0].get("cxrYgid") or matches[0].get("cxrXm") != invoice.get("cxrxm"):
                    raise ValueError("travel_order_invoice_passenger_mismatch")
                if matches[0] not in own_passengers:
                    continue
                projected = {key: invoice.get(key) for key in (
                    "id", "mxid", "appStatus", "appStatusName", "invoiceTypeName", "invoiceNo", "fpje", "fptt", "nsrsbh", "cxrxm",
                )}
                if str(invoice.get("appStatus")) == "3":
                    path = str(invoice.get("invoiceUrl") or "")
                    if not re.fullmatch(r"[0-9]+/[^/\\?#%\s]+\.(?:ofd|pdf|png|jpe?g)", path, re.IGNORECASE) or not invoice.get("invoiceNo") or not invoice.get("id"):
                        raise ValueError("travel_order_invoice_file_invalid")
                    file_id, name = path.split("/", 1)
                    projected["resource"] = {
                        "id": file_id,
                        "name": name,
                        "downloadUrl": origin + "/fcfile/fcc/opera/getFile/" + urllib.parse.quote(path, safe="/"),
                        "_travel_order_invoice": True,
                    }
                record["invoices"].append(projected)
            record["read_status"] = "read"
        except Exception as error:
            record["read_status"] = "failed"
            record["reason"] = str(error) if isinstance(error, ValueError) and str(error).startswith("travel_order_") else "travel_order_invoice_read_failed"
            record["invoices"] = []
    return records


def project(records, fields):
    resources, facts, seen = [], [], {}
    for record in records:
        if record.get("read_status") != "read":
            raise ValueError(record.get("reason") or "travel_order_invoice_read_failed")
        owners = sorted({
            str(field["path"]).rsplit(".field[", 1)[0]
            for field in fields
            if field.get("field_name") == "slvid" and str(field.get("raw_value") or field.get("value") or "") == record["expense_dbid"] and ".field[" in str(field.get("path") or "")
        })
        invoices = []
        for invoice in record["invoices"]:
            item = dict(invoice)
            resource = item.pop("resource", None)
            if resource:
                identity = (resource["downloadUrl"], item["invoiceNo"])
                previous = seen.setdefault(resource["id"], identity)
                if previous != identity:
                    raise ValueError("travel_order_invoice_file_conflict")
                item["resource_id"] = "travel_platform:" + resource["id"]
                resources.append(resource)
            invoices.append(item)
        value = json.dumps({**record, "invoices": invoices, "oa_detail_refs": owners}, ensure_ascii=False, sort_keys=True)
        facts.append({"name": "商旅订单发票", "value": value, "subject_ref": "travel_expense:" + record["expense_id"]})
    return resources, facts


def bind_resources(facts, bindings):
    for fact in facts:
        if fact.get("source") != "travel_platform" or fact.get("name") != "商旅订单发票":
            continue
        record = json.loads(fact["value"])
        for invoice in record["invoices"]:
            resource_id = invoice.get("resource_id")
            if resource_id:
                if resource_id not in bindings:
                    raise RuntimeError("resource_ownership_untrusted")
                invoice["resource_id"] = bindings[resource_id]
        fact["value"] = json.dumps(record, ensure_ascii=False, sort_keys=True)
