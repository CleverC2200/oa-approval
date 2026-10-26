---
name: oa-finance-preaudit
description: 执行 OA SQLite 预审批次及原生审核 Agent 调度，或使用当前任务的完整冻结材料形成六类财务和包装预审意见。财务只给意见，包装只校对文字。
---

# OA 财务预审

主 Agent 按下文执行批次；审核子 Agent 从“读取材料”开始，只处理指定单据，不自行领取或写 OA。海外字段及当前节点按[海外规则](../oa-overseas-invoice/SKILL.md)和独立授权处理。

## 主 Agent 执行规则

本节是补位、Agent 轮换、批次范围和停止方式的单一执行规则。启动目录与主线要求遵循[项目约定](../../../AGENTS.md)，定时任务与人工批次使用同一 `main` 入口。Python 返回任务和文件路径，原生审核 Agent 由主 Agent 创建。

### 冻结批次与领取

1. 先按 README 操作入口确定本轮绝对根目录 `OA_RUNTIME_ROOT`，通过 `bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" start --batch-size N --limit N` 重新拉取真实待办，后续操作沿用同一根目录，按当前授权选择适用类型。`--batch-size N` 将本轮全局 FIFO 排队前缀持久保存在 `runs`，启动输出的 `batch.size` 和 `batch.task_ids` 给出范围。保存 `run_id`、任务身份及顺序。`--limit` 仅控制紧凑展示，不能限制批次；省略 `--batch-size` 保留完整队列的旧调用行为。旧批次名单不跨新轮使用，历史成功和未知提交不进入重发路径。默认预览，真实写入和海外字段分别按当前明确授权执行。
2. 每次 `collect` 前检查停止状态、下一张预期任务和剩余名额。领取达到批次范围后不再请求 `collect`，技术失败也占已领取数量。程序拒绝领取范围外的第 N+1 单；批次内无排队或在途任务时返回 `done`，即使完整队列还有范围外待办。
3. 有空位时先确定最早的下一单，再选择审核 Agent。自动领取不传 `--workflow-key`；该参数只用于显式筛选。类型只影响空闲 Agent 的复用，同类积压可以占满三个名额。将原生工具返回的实际 Agent 标识、下一张 `task_id` 和该 Agent 的类型声明一起传给保护入口：`collect --agent-id <actual-native-agent-id> --expected-task-id <next-task-id> --agent-workflow-key <workflow-key>`。程序在事务内领取前检查预期身份、类型、同代类型一致及最多 5 单，拒绝时不领取，也不跳过老单。逻辑槽号只保存在主 Agent 工作表中。领取后核对本轮及预期任务身份，异常立即停止派发和写入。

总在途上限为 3。同一实际原生 Agent 同时只拥有一单。`preparing`、`reviewing`、`reviewed`、`submitting` 均占名额；审核完成但等待提交的单据也占名额。`submitted`、明确结束的 `failed` 或默认预览的 `preview` 释放名额。

命令行核对范围、下一任务或交付身份后再执行 `collect` / `submit` 时，将核对与后续操作放在同一失败即停的 shell 中，使用 `set -e` 或 `&&`；含管道时同时启用 `pipefail`。任一 `jq -e`、`test` 或 CLI 返回失败，均停止该串后续操作。退出码不替代下文对 JSON 状态、身份及回执事实的核对。此约定约束主 Agent 的临时工具命令，入口自身的身份、冻结材料与写入检查仍须通过。

### 准备、审核与补位

主 Agent 通过本机 socket 调用 `collect`。服务只启动一个采集工作进程，由它自行建立 Approval、SQLite 和 OA 读取对象，浏览器采集保持串行。原 `collect` 客户端持续等待本次最终结果；保留该工具会话，主 Agent 在等待期间接收其他审核结果，并通过同一运行服务提交、回查。第二个 `collect` 返回 `waiting` / `collection_in_progress`，不排入后台采集队列。标准输入 JSON 仍按原顺序执行，标准输入的 `collect` 为同步调用。

材料准备完成后，通过 exec 执行 parser 和整份 DeepSeek 附件读取。直接接管 `recognize` 的最终 stdout 摘要，将其中的报告路径交给审核 Agent；具体输出及长任务进度见[附件识别入口](../../../docs/agents/attachment-recognition.md)。识别运行期间，可先向审核 Agent 交付本单完整冻结材料及规则引用，完整读取规则、表单、明细和页面业务资料；识别结束后补交本单识别文件引用或具体读取失败。全部适用识别结束、审核 Agent 完整读取全部适用材料及规则并完成核对后，才能形成最终意见。主线程只交换紧凑状态、计数、文件路径及具体异常，不把全文和逐页内容回灌主线程。高影响读数的定向复读使用识别报告 `parts[].resource_id` 和 `parts[].page_number`。

收到有效完成结果时，先处理结果，再补空位；无需等另一单采集结束。主 Agent 按实际接收到有效完成结果的顺序，一次发送一个 `submit`，按下文接管提交结果；服务主循环是唯一 Writer。后领取的单先审核完成，可以先提交，较早单继续完整审核。每单结束后释放对应名额，授权批次尚有未领取单且采集空闲时立即补位。

真实 `submit` 的成功返回已包含提交、回查及持久化结果。返回 `status=submitted` 且 `submit_ok`、`done_hit`、`postcheck_done_hit` 均为 `true` 时，主 Agent 接管并保留该回执，本单提交完成。海外还核对 `fields_match=true`。对已提交任务再次调用 `postcheck` 只返回本地 `submissions` 回执，无需作为每单额外步骤。重复调用返回本地回执时，核对其中已保存的成功验证事实，不把仅有 `status=submitted` 当作新一次外部回查。

无写授权时，`submit` 保存 `preview`，结束本次预览后可以补位。有写授权时直接使用 `--confirm-write YES` 串行提交，不先积累预览。之后另行授权提交已有预览时，暂停新领取，按本次有界范围串行处理，继续核对目标和冻结材料。

收取结果时按“形成并交回意见”的一次最终交付核对实际 Agent 身份、代次、`task_id` 和冻结材料版本，旧代次消息不能覆盖新任务。队列、实际 Agent 领取计数、在途容量、下一候选及回执验收统一读取 `status --run-id <本轮 run_id> --view batch`。主 Agent 工作表只补充授权范围、逻辑槽位、原生工具会话及文件引用，计数引用公开状态快照，不另行加减维护。需要保留证据时，将原始 CLI JSON 保存到本轮 runtime；完整正文和逐页内容由审核 Agent 从文件完整读取。

采集或识别尚未返回时，保留原工具会话，先处理已到达的审核结果和其他独立工作。没有可处理结果时，每次等待进程结果约 30 秒；多个独立进程可并行等待，提交仍串行。进程结束后接管其最终返回，不再为相同状态和路径追加文件查询；只有工具会话未结束且需要识别进度时才使用公开 `progress`。

名额已满且没有可处理结果时，等待原生完成通知，每次等待 30 至 60 秒。只传新增事实或阻断，不以轮询意见文件判断完成，也不催稿。

`collect` 未领到任务时只使用紧凑的 `status`、`reason` 和可用的 `summary` 状态计数判断。`waiting` 表示采集占用、仍有排队或在途任务，先处理结果或等待；`done` 表示本批排队和在途均耗尽；`blocked` 停止本轮补位和写入。`refused` 表示保护校验未通过，核对预期任务和 Agent 代次后再决定下一步。不能把 `waiting` 当作批次完成。

### Agent 轮换

优先复用同类、空闲且上下文仍短的 Agent；有多个时选本代已处理数最少者。每代最多处理 5 单，预览和技术失败也计入。达到上限、换类型，或本单材料特别大、读取质量受上下文影响时，当前单收尾后轮换。

新建时使用 `fork_turns=none`，继承默认模型设置，只发送匹配规则入口和本单材料/识别文件引用，不继承主线程、上一代或此前单据的业务历史。同类复用时，由主 Agent 使用原生后续任务入口启动下一单，只补本单定位，不重复粘贴旧意见。每代使用新的实际原生 Agent 身份，数据库的 `agent_id` 随之更新。

轮换在当前意见和观察完整交回、主 Agent 接管且本单处理结束后进行；保留已有意见、材料和 SQLite 事实。旧代停止接单，不中断当前单或重复提交。进程或主会话中断后，下一轮重新拉取待办，不恢复旧审核对话。

### 技术失败、未知提交与停止

审核出现技术失败时，先核对原生结果或状态，保存具体原因。通过 `fail --task-id ... --reason ...` 登记本地失败；服务只接受当前 `running` 轮的 `preparing`/`reviewing` 任务，不访问 OA。已经形成意见、开始提交或结果未知的任务不能用它释放。采集异常由现有采集入口记录本单失败；等待较久本身不构成技术失败。不得直接改数据库或编造财务意见释放名额。

提交结果为 `unknown` 或仍为 `submitting` 时，只使用 `postcheck --task-id ...` 只读核对外部结果并保存回查事实，不重发。`unknown` 阻断整轮，立即停止补位和其他提交，保留全部意见及回执。正在采集的任务安全结束后保留材料，采集完成或回查成功均不自动恢复本轮调度。

用户要求停止时，立即停止补位。已领取的最多 3 单按当前授权完成审核、提交或预览；未知结果按上一段只读回查。核对后按下节关闭整轮，收尾前保持原进程标准输入打开。用户明确要求立即停止时，不再发起新写入，已开始且结果未知的写入只读回查。

### 正常完成与关闭轮次

使用本轮保存的 `batch.task_ids` 逐单验收，未设置 `--batch-size` 时以本轮完整队列为范围。确认每单已经提交并有成功验证事实、完成预览或明确登记失败；本批没有未领取或在途任务。范围外的 `queued` 不计入本批完成条件。停止排空只核对已领取单，未领取单保留待办；未知提交单独报告阻断及回查结果。

沿用本轮固定的绝对 `OA_RUNTIME_ROOT` 和实际 `run_id`，通过项目解释器执行公开命令；`OA_RUN_ID` 取本轮启动结果：

```bash
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" status --run-id "${OA_RUN_ID:?}"
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" finish --run-id "${OA_RUN_ID:?}"
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" status --run-id "${OA_RUN_ID:?}"
```

`finish` 等待当前采集完成并落盘，再关闭本轮、socket 并释放 `approval.lock`；正常批次先完成逐单验收再调用它。保留关闭回执，等待原 owner 工具会话退出，最后确认指定轮次 `run.status=closed` 且 `owner.reachable=false`。`finish` 返回 `closed` 只证明轮次关闭，本批完成仍以逐单验收为准。使用上述公开查询收尾，无需临时读库。EOF 同样触发安全关闭；采集客户端断开仍保留已取得材料，采集进程异常登记技术失败，不生成业务意见。

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

结构化内容已足够或页面字节完全相同时可复用读取结果，仍分别保留每个逻辑资源和页面的归属。视频和不支持格式明确未读取。官方附件来源已完整取得且明确没有可下载附件时，识别工具返回 `status=skipped`，仅跳过附件识别，继续完整审核表单、明细及页面业务资料。采集失败、材料全空或声明附件未取得时报告技术失败，不把读取失败写成申请人未提供；已取得的真实业务缺项和关系不明按规则用需人工判断意见表达。

大图需要辅助副本时使用[图片压缩工具](../../../docs/agents/image-compression.md)。辅助副本不替代原件，小字、金额、日期或印章不清时回到原件或局部复读。

## 形成并交回意见

按匹配规则完成全部审核项和明细覆盖，再按[共享结果格式](references/result-contract.md)写简短签字意见。正常项只给结果，有问题的项保留具体日期、地点或明细、异常或缺口及必要处理；完整核对依据不直接复制进签字意见。业务异常不等于批准、退回或技术失败，脚本不替 Agent 生成或压缩业务判断。

将最终意见保存为本单独立文本文件。附件识别报告保留真实资源、页码/区域/单元格位置及来源；人工复读、配对和验算依据按[执行接口](references/execution-contract.md)记录为附件观察，与意见一并交回主 Agent，使用适用的 `--recognition-file`、`--observations-file` 送入现有 `submit`，两者可同时提供。仅表单或商旅页面的事实保留冻结来源与本次核对记录，不虚构附件归属。主 Agent 通过 `submit --task-id ... --opinion-file ...` 保存意见及适用的识别、观察文件。默认预览；实际提交需要当前明确授权及 `--confirm-write YES`。

审核 Agent 以一次最终回复交回有效完成结果，包含原生工具分配的真实 Agent 身份、本代身份、本单 `task_id`、冻结材料版本及最终意见、观察/识别文件的绝对路径。材料版本引用本次 `collect` 返回的唯一 `material_file` 路径；需要核对内容摘要时使用公开 `show --task-id ...` 返回的 `task.material_sha256`。无适用的识别或观察文件时说明原因，不编造文件或来源。

有效最终交付即结束当前审核轮，等待主 Agent 接管；同一结果无需再发送第二份完成通知。具体新增缺口或技术阻断可在发现时单独通知。主 Agent 按执行规则核对交付、提交并保留回执，需要复用该 Agent 时再以原生后续任务启动下一单。
