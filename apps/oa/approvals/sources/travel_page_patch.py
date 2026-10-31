from __future__ import annotations

import gzip
import hashlib
import zlib

COST_PATH = "/fcc/fcecf/defaultv2/js/bxdHome/modules/costTotal.js"
UTIL_PATH = "/static/core/util/jquery.util.js"
COST_HASH = "4f96e4108fbbb01a31e0d17d495080c5efa3628f2dcce341d86f7a12699e4076"
UTIL_HASH = "fc9feadde7a757b4a5468c2fd556045ddd996b79903626deb4951bbbb1bbe9d5"
ANCHOR = (
    "\tfunction getRenderData(data, onlyRender, eventData) {\n\t\tdata = data || {};"
)
INSERT = "\n\t\tif (data.cwspbzje === undefined && bxdAppData.fkBxdKzb && bxdAppData.fkBxdKzb.cwspbzje !== undefined) {\n\t\t\tdata.cwspbzje = bxdAppData.fkBxdKzb.cwspbzje;\n\t\t}"
STATE_INSERT = "\n\tvar readonlySummaryOwner = '';\n\tvar readonlySummaryValues = {};\n\tvar readonlyRenderGeneration = 0;\n"
MERGE_INSERT = "\n\t\tvar readonlyOwner = JSON.stringify([bxdAppData.bxdzb.bxdh, bxdAppData.bxdzb.bxpch]);\n\t\tif (readonlySummaryOwner !== readonlyOwner) {\n\t\t\treadonlySummaryOwner = readonlyOwner;\n\t\t\treadonlySummaryValues = {};\n\t\t}\n\t\t['hjdkse', 'hjbhsje', 'fzpseTotal', 'zce', 'cwxtsjfkje', 'fpyqe'].forEach(function (key) {\n\t\t\tif (data[key] === undefined && Object.prototype.hasOwnProperty.call(readonlySummaryValues, key)) {\n\t\t\t\tdata[key] = readonlySummaryValues[key];\n\t\t\t} else if (data[key] !== undefined) {\n\t\t\t\treadonlySummaryValues[key] = data[key];\n\t\t\t}\n\t\t});"
ORIGINAL_MSG = "layer.msg('该模块无权限，请联系管理员！');"
REPLACEMENT = "if (XMLHttpRequest.getResponseHeader('X-OA-Readonly-Blocked') === '1') {\n                                window.__oaReadonlyDenied = window.__oaReadonlyDenied || [];\n                                window.__oaReadonlyDenied.push({request_id: XMLHttpRequest.getResponseHeader('X-OA-Readonly-Request-Id'), status: 403});\n                                layer.msg('独立只读测试已拦截请求，详见采集诊断');\n                            } else {\n                                layer.msg('该模块无权限，请联系管理员！');\n                            }"


def _verify(data, expected):
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("travel_compatibility_version_mismatch")


def _patch_cost(data):
    _verify(data, COST_HASH)
    text = data.decode().replace(ANCHOR, ANCHOR + INSERT, 1)
    replacements = [
        ("\tvar renderData = {};", "\tvar renderData = {};" + STATE_INSERT),
        (ANCHOR + INSERT, ANCHOR + INSERT + MERGE_INSERT),
        (
            "Utils.transCNY(data.yfjje)",
            "Utils.transCNY(data.yfjje === 0 ? '0' : data.yfjje)",
        ),
        (
            "\tfunction renderHtml(data, yfjje, clfzje) {\n\t\tvar $deferred = $.Deferred();\n\t\tgetTmp().then(function () {",
            "\tfunction renderHtml(data, yfjje, clfzje) {\n\t\tvar readonlyGeneration = ++readonlyRenderGeneration;\n\t\tvar $deferred = $.Deferred();\n\t\tgetTmp().then(function () {\n\t\t\tif (readonlyGeneration !== readonlyRenderGeneration) { $deferred.resolve(); return; }",
        ),
        (
            "laytpl(htmls).render(_params, function (html) {\n\t\t\t\t$('#costTotal').html(html);",
            "laytpl(htmls).render(_params, function (html) {\n\t\t\t\tif (readonlyGeneration !== readonlyRenderGeneration) { $deferred.resolve(); return; }\n\t\t\t\t$('#costTotal').html(html);",
        ),
    ]
    for old, new in replacements:
        if text.count(old) != 1:
            raise ValueError("summary_patch_anchor_mismatch")
        text = text.replace(old, new, 1)
    return text.encode()


def _patch_util(data):
    _verify(data, UTIL_HASH)
    text = data.decode()
    if text.count(ORIGINAL_MSG) != 1:
        raise ValueError("travel_compatibility_anchor_mismatch")
    return text.replace(ORIGINAL_MSG, REPLACEMENT, 1).encode()


def patch_response(*, path, host, trip_host, status, data, headers):
    if host != trip_host or status != 200 or path not in {COST_PATH, UTIL_PATH}:
        return data, headers, None
    encoding = next(
        (v for k, v in headers if k.lower() == "content-encoding"), ""
    ).lower()
    if encoding == "gzip":
        data = gzip.decompress(data)
    elif encoding == "deflate":
        data = zlib.decompress(data)
    elif encoding not in {"", "identity"}:
        raise ValueError("travel_compatibility_encoding_unsupported")
    before = hashlib.sha256(data).hexdigest()
    data = _patch_cost(data) if path == COST_PATH else _patch_util(data)
    headers = [
        (k, v)
        for k, v in headers
        if k.lower()
        not in {
            "content-length",
            "content-encoding",
            "etag",
            "last-modified",
            "cache-control",
        }
    ]
    headers.append(("Cache-Control", "no-store"))
    return (
        data,
        headers,
        {
            "path": path,
            "before_sha256": before,
            "after_sha256": hashlib.sha256(data).hexdigest(),
        },
    )
