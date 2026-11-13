# 流程预测与财务前置标记

不启动审批轮次，查看 OA 原生的完整节点、办理人及已完成/当前/预测/未知状态，按私有名单标记财务前置处理需求。

## Sub-features

- `predict-text`：终端可读的完整路径。
- `predict-json`：节点、办理人、原始响应及完整性。
- `refresh`：请求重新预测。
- `finance-precheck`：标记建议动作；真实 submit 再核对并执行获准动作。

## How to get to it (user POV)

- `bash .codex/python.sh -m apps.oa approvals predict --help`
- `bash .codex/python.sh -m apps.oa.approvals predict --help`
- 具体字段和配置合同见 [OA API](../../../../docs/agents/oa-api.md#流程智能预测)。

## Driving it with OA CLI

Preconditions:

- 当前授权允许访问指定真实申请，既有 OA API 身份与私有配置可用。不要拿另一单或旧截图代替。

- **文本。** `predict --request-id "$OA_REQUEST_ID"`，ID取本次授权目标。检查全部节点和办理人，未知状态保留未知。
- **JSON。** 相同目标增加 `--format json`，核 complete、目标身份、节点与办理人及原始响应；与文本展示一致。
- **刷新。** 只有本次需要重新预测才加 `--refresh`，记录请求和实际结果。
- **离线回归。** `bash .codex/python.sh -m pytest -q tests/approvals/test_finance_precheck.py`，不计为真实 OA 接口验收。

## Gotchas

- predict 可能在 OA 生成或更新预测缓存，不能称作完全无外部写入的 doctor。
- 预测不保存表单或提交审批；标记需财务前置也不等于已转办。
- 真正提交/转办仍经过审批当前目标、意见、授权及回执保护；路径或办理人不明时停止。
