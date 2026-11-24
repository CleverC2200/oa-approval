# 共享来源故障只暂缓受影响的待办

共享权威来源已确认暂不可用时，在已冻结批次内按原接收顺序暂缓明确依赖该来源或适用性尚未核清的单据，继续处理明确无需该来源的单据。来源恢复后，暂缓单据保留原接收时间并恢复优先顺序。这样可以保持受影响单据的位置，同时让本批无关待办继续。

在领取前判定的暂缓保留待办状态，故障证据随批次可查。来源可用性可以作为批次决策依据。材料与开票名称基准按[财务观察规则](../../.agents/skills/oa-finance-preaudit/references/finance-observation-contract.md#飞书开票名称基准)逐单取得。批次范围、真实写入授权及未知提交保护沿用[批次执行规则](../../.agents/skills/oa-finance-preaudit/references/batch-execution.md)。使用两个依赖该来源的待办和一个无关待办验收停领范围、恢复顺序和独立取证。 [Issue #128](https://github.com/CleverC2200/oa-approval/issues/128)
