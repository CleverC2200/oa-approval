# 海外采购发票回填

本流程沿用独立授权，业务规则由[海外发票 Skill](../.agents/skills/oa-overseas-invoice/SKILL.md)维护。新 CLI 与其他审批共用 SQLite 队列，写入适配单独处理，不批准付款。

collect 保存当前表单、明细与每行 scfp 对应的原件。Agent 阅读本单所有原件，每个值附页码和原文短引，无法确认的值保持未知；权威无发票的行保留全部字段。

识别数据由 build_plan 校验：material_digest 绑定本次材料；invoices 数组的每项代表一张发票，包含 resource_id、sha256、currency 及 Skill 列出的五个识别字段。同一原件含多张发票时允许重复 resource_id 和 sha256，以不同发票号码区分；所有原件都必须被完整识别。各字段提供 status、value、page 和 quote，分别保存识别状态、值、原件页码和原文短引；缺失值使用 missing、null 并保留对应页码及短引。具体输入形状由 apps/oa/approvals/overseas.py 的纯业务函数维护，行内汇总及业务错误处理以 Skill 为准。

submit 只允许修改当前表单明确映射的五个发票字段，生成当前明细记录的 detailData 后提交当前 RPA 节点。新增的发票日期、发票采购订单号按表单同名显示标签精确映射；映射缺失、歧义或不可编辑时停止，不猜测字段代码。人工所选采购订单号、其余字段、附件与付款状态不修改。实际写入要求当前明确确认及独立海外授权；默认 preview 不提交 OA。

成功要求授权字段回读一致、其他表单内容与附件未变、已办命中及待办消失。未知或回读冲突只使用 postcheck 读取结果，不自动重发。

本项目当前验证使用本地表单和 fake OA，未执行真实字段回填或业务验收。
