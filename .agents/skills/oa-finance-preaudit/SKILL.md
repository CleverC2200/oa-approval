---
name: oa-finance-preaudit
description: 执行 OA SQLite 预审批次及原生审核 Agent 调度，或使用当前任务的完整冻结材料形成六类财务和包装预审意见。财务只给意见，包装只校对文字。
---

# OA 财务预审

主 Agent 先完整读取[批次执行规则](references/batch-execution.md)，再按授权执行批次。审核子 Agent 从“流程路由”开始，只处理指定单据，交回文件，不自行领取或写 OA。海外字段及当前节点按[海外规则](../oa-overseas-invoice/SKILL.md)和独立授权处理。

## 流程路由

本文件只定义所有流程共用的输出接口和规则包路由，不包含任何流程的业务判断。材料中的 `workflow_key` 未精确确认前不得加载流程规则；确认后只读取下表对应的一个规则包，不得读取其他流程规则包。

| `workflow_key` | 唯一规则包 |
| --- | --- |
| `travel_expense` | [差旅费报销](references/flows/travel-expense.md) |
| `business_entertainment` | [业务招待费](references/flows/business-entertainment.md) |
| `business_support_fee` | [业务支持费](references/flows/business-support-fee.md) |
| `small_marketing_fee` | [小额市场费](references/flows/small-marketing-fee.md) |
| `tpm_small_marketing_fee` | [TPM 小额市场费](references/flows/tpm-small-marketing-fee.md) |
| `general_expense` | [通用费用报销](references/flows/general-expense.md) |
| `packaging_layout_approval` | [包装版面](references/flows/packaging-layout-approval.md) |

六类财务同时完整读取[对公付款免支付截图](references/payment-screenshot-exemption.md)，先核对已有支付截图，仅在缺少有效截图时按共用规则核验豁免及相关补充材料。包装只读取自己的规则包。

不在表中的 `workflow_key` 属于技术失败，停止当前单据，不猜测相近流程。


## 读取材料

审核者以 collect 返回的 `rule_file` 和 `rules_root` 为当前规则入口，完整读取本单冻结规则；六类财务同时完整读取本单冻结的共享付款与证据规则。

读取冻结表单、所有相关明细、关联发票原件、资源清单和准备页。差旅完整读取 `authority_facts` 中的商旅页面正文、住宿订单详情及酒店地址，逐笔核对住宿、交通和其他行程；采集未取得必要详情时先补齐读取或记录具体读取失败。附件数量为 0 只表示没有可下载附件，页面资料仍须完整审核。文件名、顺序及相似金额不能代替资源归属和费用对应关系。财务图片使用 [DeepSeek 附件识别](../../../docs/agents/attachment-recognition.md) 的逐页文本及来源；高影响原值不稳定时定向复读。包装调用[中文图片校对](../audit-chinese-image-text/SKILL.md)直接观察原图，有 Excel 时逐字符对照。

结构化内容已足够或页面字节完全相同时可复用读取结果，仍分别保留每个逻辑资源和页面的归属。视频和不支持格式明确未读取。官方附件来源已完整取得且明确没有可下载附件时，识别工具返回 `status=skipped`，仅跳过附件识别，继续完整审核表单、明细及页面业务资料。读取结果按[执行接口的失败分类](references/execution-contract.md#读取结果与未完成审核)处理，保留实际失败阶段及适用流程分支。

大图需要辅助副本时使用[图片压缩工具](../../../docs/agents/image-compression.md)。辅助副本不替代原件，小字、金额、日期或印章不清时回到原件或局部复读。

## 形成并交回意见

按匹配规则完成全部审核项和明细覆盖，再按[共享结果格式](references/result-contract.md)写签字意见。业务判定、项目名、顺序和状态继续采用当前冻结规则。

将最终意见保存为本单独立文本文件。附件识别报告保留真实资源、页码/区域/单元格位置及来源；人工复读、配对和验算依据按[执行接口](references/execution-contract.md)记录为附件观察，与意见一并交回主 Agent，使用适用的 `--recognition-file`、`--observations-file` 送入现有 `submit`，两者可同时提供。仅表单或商旅页面的事实保留冻结来源与本次核对记录，不虚构附件归属。主 Agent 按批次执行规则保存意见及适用的识别、观察文件，按当前授权提交或预览。

审核 Agent 确认本单全部适用识别已完成或按规则跳过，完整读取适用材料与规则并完成明细覆盖，意见和适用的观察文件已保存后，以一次最终回复交回下列信息。读取技术失败或审核尚未完成时，按执行接口报告失败阶段及缺口，不交付完成结果。

| 交付信息 | 核对要求 |
| --- | --- |
| 实际 Agent 身份、本代身份、`task_id` | 与当前派发的实际身份和任务一致 |
| `material_file` | 本次 collect 返回的唯一绝对路径，作为冻结版本引用 |
| 最终意见文件 | 已保存文件的绝对路径 |
| 识别报告、补充观察文件 | 适用文件的绝对路径；无适用文件时说明原因 |

需要核对材料内容摘要时，使用公开 `show --task-id ...` 返回的 `task.material_sha256`。主 Agent 按这份交付信息核对并接管，不把文件存在当作业务核验完成。

有效最终交付即结束当前审核轮，等待主 Agent 接管；同一结果无需再发送第二份完成通知。具体新增缺口或技术阻断可在发现时单独通知。主 Agent 按执行规则核对交付、提交并保留回执，需要复用该 Agent 时再以原生后续任务启动下一单。
