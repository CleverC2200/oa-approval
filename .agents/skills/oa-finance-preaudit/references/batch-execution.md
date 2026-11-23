# 批次执行规则

本节是补位、Agent 轮换、批次范围和停止方式的单一执行规则。启动目录与主线要求遵循[项目约定](../../../../AGENTS.md)，定时任务与人工批次使用同一 `main` 入口。Python 返回任务和文件路径，原生审核 Agent 由主 Agent 创建。小时定时批次与独立接续对话同时执行[连续处理规则](hourly-dispatch.md)。

## 冻结批次与领取

1. 先按 README 操作入口确定本轮绝对根目录 `OA_RUNTIME_ROOT`，通过 `bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" start --batch-size N --limit N` 重新拉取真实待办，后续操作沿用同一根目录，按当前授权选择适用类型。`--batch-size N` 将本轮全局 FIFO 排队前缀持久保存在 `runs`，启动输出的 `batch.size` 和 `batch.task_ids` 给出范围。保存 `run_id`、任务身份及顺序。`--limit` 仅控制紧凑展示，不能限制批次；省略 `--batch-size` 保留完整队列的旧调用行为。旧批次名单不跨新轮使用，历史成功和未知提交不进入重发路径。默认预览，真实写入和海外字段分别按当前明确授权执行。
2. 每次 `collect` 前检查停止状态、下一张 FIFO 任务和准备缓冲。`collect --expected-task-id <next-task-id>` 不传 Agent 身份，只采集和冻结一份待审材料。领取达到批次范围后不再请求 `collect`，技术失败也占已领取数量。程序拒绝领取范围外的第 N+1 单；批次内无排队或在途任务时返回 `done`，即使完整队列还有范围外待办。
3. 完成本单适用准备及识别后，核对审核空位与结果背压，再选择真实审核 Agent。使用 `dispatch-review --expected-task-id <prepared-task-id> --agent-id <actual-native-agent-id> --agent-workflow-key <workflow-key>`，财务图片同时传本次完整的 `--recognition-file`。包装沿直接观察原图的分支，纯结构化材料可直接读取；海外按专用规则处理。入口核对当前材料版本、适用识别完整性、实际身份、类型和同代类型一致后才绑定审核槽。拒绝时保留材料，不跳过老单。类型只影响空闲 Agent 复用，同类积压可以占满审核槽。逻辑槽号只保存在主 Agent 工作表中。

用户明确要求紧急处理时，在新批次的 `start` 加 `--review-slots 4`，含合法接续。普通批次默认 3 个审核槽，接续未指定时沿用前批容量。以 `status.capacity` 和本次实际可启动的 Agent 数量安排派发。

| 阶段 | 容量与释放条件 |
| --- | --- |
| `preparing`、`prepared` | 独立准备缓冲 1 单，不占审核槽 |
| `reviewing` | 以 `review_limit` 为上限。每个真实 Agent 同时一单，`deliver` 保存成功或明确技术失败才释放 |
| `reviewed`、`submitting` | 结果容量为审核槽数加 3。已保存的结果达到 3 单时停止新派发，在审任务各预留一个结果位置，已完成交付继续落盘 |
| 唯一 Writer | 串行处理已保存结果的提交、预览与回查，不占审核槽 |

`backpressure=true` 时先排结果，不再派发；准备缓冲可按入口判断独立补齐。正常活动 Writer 的 `submitting` 不阻止采集、派发或本地交付。未知或失联的未结束提交按下文停止保护执行。所有未结束阶段都参与关闭检查，审核槽释放不表示整单结束。

命令行核对范围、下一任务或交付身份后再执行 `collect` / `dispatch-review` / `deliver` / `submit` 时，将核对与后续操作放在同一失败即停的 shell 中，使用 `set -e` 或 `&&`；含管道时同时启用 `pipefail`。任一 `jq -e`、`test` 或 CLI 返回失败，均停止该串后续操作。退出码不替代下文对 JSON 状态、身份及回执事实的核对。此约定约束主 Agent 的临时工具命令，入口自身的身份、冻结材料与写入检查仍须通过。

## 补位循环

本轮正常运行时，每次收到采集、识别、审核交付或 Writer 结果，都接管已到达的回执，并检查下面各项。一次检查可以发起多个独立动作。每次派发或交付后重新核对容量，直到本轮暂时没有可推进的动作才等待。

| 本轮状态 | 立即推进 |
| --- | --- |
| 收到绑定当前任务的完整审核结果 | 核对并 `deliver`，释放审核槽。Writer 正忙时也继续保存交付 |
| FIFO 待审材料和适用识别就绪，`capacity.review_available > 0` 且 `backpressure=false` | `dispatch-review` 并立即给实际审核 Agent 发任务。Writer 忙时照常派发，同类型有多单时可用多个 Agent |
| `next_candidate` 存在，准备缓冲有空位，且没有活动采集 | `collect` 下一张 FIFO 单，采集完成后立刻执行适用识别。审核槽全满时也提前准备一单 |
| 有已保存的审核结果且 Writer 空闲 | 按授权发起一次 `submit` 或 `preview`，保存会话并继续推进其他阶段 |

长操作用独立工具会话启动，有返回时间参数时用短初始返回（如 `yield_time_ms=1000`），记录会话 ID 并带着它继续补位。待审材料就绪时优先派发；采集或识别仍在跑时推进已完成交付或空闲 Writer。每个阶段按自己的容量推进，提交回执只占用 Writer。用公开状态区分有空位但需要等材料、结果背压、范围耗尽、技术或授权保护，以及执行器仍有动作可做。未知写入和失联 Writer 仍按停止保护处理。

## 阶段操作

主 Agent 通过本机 socket 调用 `collect`。浏览器采集保持串行。原 `collect` 客户端持续等待本次最终结果；保留该工具会话，主 Agent 在等待期间接收其他审核结果，并通过同一运行服务提交、回查。第二个 `collect` 返回 `waiting` / `collection_in_progress`，不排入后台采集队列。标准输入 JSON 保持顺序，在执行下一条前等待活动采集与 Writer 的最终结果；需要并行处理时使用 socket。

材料准备完成后，通过 exec 执行 parser 和本流程适用的整份附件识别。识别保持独立工具会话，不占审核槽；完整识别结束或规则明确允许跳过后，才调用 `dispatch-review` 并派发审核。直接接管 `recognize` 最终 stdout 摘要和报告路径，输出及长任务进度见[附件识别入口](../../../../docs/agents/attachment-recognition.md)。审核者再完整读取本单规则、表单、明细、页面资料及适用识别报告，高影响读数按来源定向复读，完成全部适用核对后形成最终意见。

收到有效完成结果时，先核对实际身份、代次、任务和冻结材料版本，立即通过 `deliver` 保存意见、识别、观察及[审核结果文件](review-assessment.md)。只有返回 `status=reviewed`、保存的 `review_id` 及证据路径后才能复用审核 Agent；保存失败时保留原任务与文件，按具体错误处理。按有效交付顺序接管结果，空闲 Writer 消费已保存交付。主 Agent 一次发送一个 `submit` 或 `preview`，保留原工具会话直到最终回执；第二个 Writer 请求返回 `waiting` / `writer_in_progress`，不排入另一条后台队列。

状态、采集、审核派发与本地交付使用独立连接。后领取的单先完成，可以先提交，较早单继续完整审核。待接管结果未清空时，性能统计、诊断脚本和规则整理留待排空关闭后处理。

真实 `submit` 的成功返回已包含提交、回查及持久化结果。返回 `status=submitted` 且 `submit_ok`、`done_hit`、`postcheck_done_hit` 均为 `true` 时，主 Agent 接管并保留该回执，本单提交完成。海外还核对 `fields_match=true`。对已提交任务再次调用 `postcheck` 只返回本地 `submissions` 回执，无需作为每单额外步骤。重复调用返回本地回执时，核对其中已保存的成功验证事实，不把仅有 `status=submitted` 当作新一次外部回查。

无写授权时，先 `deliver`，再用 `preview --task-id <ID>` 或默认 `submit` 保存预览状态。有写授权时，核对已保存交付后用 `submit --task-id <ID> --confirm-write YES` 串行提交，不先积累预览；海外同时要求独立授权及 `--allow-overseas YES`。`submit` / `preview` 只消费已保存交付，不接受意见、识别、观察或审核结果文件。之后另行授权提交已有预览时，暂停新领取，按本次有界范围串行处理，继续核对目标和冻结材料。

收取结果时按[审核交付](../SKILL.md#形成并交回意见)的一次最终交付核对实际 Agent 身份、代次、`task_id` 和冻结材料版本，旧代次消息不能覆盖新任务。队列、实际 Agent 领取计数、在途容量、下一候选及回执验收统一读取 `status --run-id <本轮 run_id> --view batch`。主 Agent 工作表只补充授权范围、逻辑槽位、原生工具会话及文件引用，计数引用公开状态快照，不另行加减维护。主 Agent 将 CLI 原始 JSON 保存到本轮 runtime，工具返回只投影状态、计数、任务身份、文件路径和必要错误；整份材料、逐页识别、观察和意见正文由审核 Agent 从文件读取。定期验收只展示批次汇总，逐单事实保留在原始状态文件中。

关闭后需要核对耗时时，使用 `bash .codex/python.sh .codex/approval_timings.py --case-dir <本轮私有目录>`。它读取已保存的 `delivery-*` 实际交付路径、`collect-*`、`preserved-*` 和 `submit-*` 回执；不从审核结果的可选字段推断路径，原路径清理后仍可读取保全副本。文件时间仅供诊断，不替代原生交付时间、材料覆盖或审核结论。

补位循环已无可推进动作时，等活动工具结果或原生审核完成通知，每次约 30 秒。多个独立进程可并行等待；一有结果就返回补位循环。进程结束后直接接管最终返回，识别仍在运行且需要查看进度时使用公开 `progress`。原生审核以完成通知为准，进展反馈只传新增事实或阻断。

审核期间通过 `status --view batch` 的 `review_progress` 查看读取状态、版本匹配记录数、更新时间和进度文件；完整 Pointer 与范围按需从对应 `read-progress.json` 有界读取，不展开到主线程状态摘要。读取记录只证明已提供范围，不以 owner 可达代替审核进展。原生工具能够提供最后事件时间时，连续10分钟没有新工具事件或阶段结果，核对实际 Agent、任务、最后事件和仍运行的工具进程。无法取得精确事件时间时说明缺口，不估造停滞时长。

模型响应出现分钟级空档时，先核对 Codex 已有本地日志中的容量、限流及重试错误；会话 JSONL 或界面静默不等于完整请求耗时。已记录模型容量退避和下一次重试时间时，保留原会话与错误证据，按实际重试状态等待，不以该等待判定材料读取失败、上下文不足或无进展后中断。日志仅读取必要时间、错误及重试元数据；用户停止和未知提交的保护继续生效。

明确失败或中断终态时保存实际错误，按下文登记技术失败。仍为 running 时向该审核 Agent 发送一次接管请求，要求保存已核依据、剩余项目及当前工具会话，不要求提前交完成意见。等待60秒后复查原生事件；有新进展则继续原审核。仍无新事件且已核实没有运行中的采集、识别、复读或其他外部工具进程时，使用原生中断入口仅取消该审核 Agent，等待其明确终止，再保存原生终态和本单进度并登记 `incomplete`。保留 owner、其他审核和全部文件，继续处理其他单据。工具进程仍运行时按其既有时限接管原会话，不重开同一工具。时间本身不直接登记失败，必须取得实际中断或失败终态；存在未知提交时继续按下文只读回查。

`collect` 未领到任务时只使用紧凑的 `status`、`reason` 和可用的 `summary` 状态计数判断。`waiting` 表示采集占用、仍有排队或在途任务，先处理结果或等待；`done` 表示本批排队和在途均耗尽；`blocked` 停止本轮补位和写入。`refused` 表示保护校验未通过，核对预期任务和 Agent 代次后再决定下一步。不能把 `waiting` 当作批次完成。

## Agent 轮换

优先复用同类、空闲且读取连续性良好的 Agent，不按固定单数强制轮换。换流程、实际上下文不足或已出现读取覆盖问题时，在本单安全收尾后换代；材料多或处理时间长本身不证明读取质量下降。

新建时使用 `fork_turns=none`，继承默认模型设置；每代使用新的实际原生 Agent 身份，数据库的 `agent_id` 随之更新。适用准备及识别完整后按下列模板派发本单。新 Agent 可先仅接收待命角色以取得真实原生身份，不能在 `dispatch-review` 通过前开始本单审核。首次任务带齐全部信息，复用同类空闲 Agent 时用原生后续任务入口替换本单身份与文件引用。

```text
角色：指定单据的审核 Agent，只交回审核结果，不领取或写 OA。
执行入口：<本项目 oa-finance-preaudit/SKILL.md 的绝对路径>，从“流程路由”开始执行；业务规则以本次冻结版本为准。
任务：<task_id>；流程：<workflow_key>；本代身份：<当前代次>。实际原生 Agent ID 沿用本次派发身份。
材料：<collect 返回的 material_file 绝对路径>。
正文读取：先读本次冻结 review-assessment.md 的“有界读取”，按业务记录定位完整原文，使用有界页及本单read-progress.json；规则与材料分别读取。
冻结规则：<rule_file>；完整规则根目录：<rules_root>。
识别：<适用完整识别的最终报告路径及状态 / 不适用及原因>。
交付：按执行入口“形成并交回意见”的完成条件及字段交回，仅引用本单文件。
```

模板只携带当前单据定位。审核完成条件和最终交付字段由 Skill 维护，不复制主线程、上一代或此前单据的业务历史。

轮换在当前意见和观察完整交回、`deliver` 成功释放审核槽后进行；保留已有意见、材料和 SQLite 事实。旧代停止接单，不中断当前单或重复提交。进程或主会话中断后，下一轮重新拉取待办，不恢复旧审核对话。

## 技术失败、未知提交与停止

审核出现技术失败时，先核对原生结果或状态，保存具体原因。冻结材料已形成时，通过 `fail --task-id ... --reason ... --review-file ...` 登记绑定的审核失败；没有冻结材料时沿用原 fail 入口。服务只接受当前 `running` 轮的 `preparing`/`prepared`/`reviewing` 任务，不访问 OA。已经形成意见、开始提交或结果未知的任务不能用它释放。采集异常由现有采集入口记录本单失败；等待较久本身不构成技术失败。不得直接改数据库或编造财务意见释放名额。

正常活动 Writer 的 `submitting` 保留原工具会话等待最终结果，不并发回查。提交为 `unknown`，或 Writer 已退出而遗留未结束提交时，只使用 `postcheck --task-id ...` 只读核对外部结果并保存回查事实，不重发。`unknown` 阻断整轮，立即停止补位和其他提交，保留全部意见及回执。正在采集的任务安全结束后保留材料，采集完成或回查成功均不自动恢复本轮调度。

用户要求停止时，立即停止采集和新派发，按当次停止指令处理各阶段。允许排空时接管在审交付并按当前授权完成已保存结果的提交或预览；未知结果按上一段只读回查。核对后按下节关闭整轮，收尾前保持原进程标准输入打开。用户明确要求立即停止时，不再发起新外部写入；当前 Writer 安全落下回执，未结束或未知写入只读回查。关闭前保留已保存结果与证据，使用 `finish --stop-file` 记录 `user_stop`，不因审核槽已释放而遗漏 `reviewed`。

## 正常完成与关闭轮次

使用本轮保存的 `batch.task_ids` 逐单验收，未设置 `--batch-size` 时以本轮完整队列为范围。公开 `status --run-id <本轮ID> --view batch` 的 `closeout.drained=true` 表示没有活动采集或 Writer、准备中或待审材料、在审任务、等待处理结果或未核实提交；`batch_complete=true` 还要求本批没有未领取单。范围外的 `queued` 不计入本批完成条件。停止排空只核对已领取单，未领取单保留待办；未知提交单独报告阻断及回查结果。执行者忙碌或探测结果不明时，`owner_state_unconfirmed` 表示暂不能确认排空或退出，先按小时反馈规则复查。

沿用本轮固定的绝对 `OA_RUNTIME_ROOT` 和实际 `run_id`，`OA_RUN_ID` 取本轮启动结果。先用公开 status 核对逐单验收，再选择下表中的一个关闭分支；不并发发送或在结果不明时重复发送 finish。保护拒绝后先处理阻断，有新事实再重新验收并收尾。

| 当前分支 | 关闭命令 |
| --- | --- |
| 小时任务或其接续对话，当前任务已明确授权通过后续小时对话接续，计划仍为 ACTIVE，且没有停止指令或未知提交 | `bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" finish --run-id "${OA_RUN_ID:?}" --require-drained --handoff`，随后按[接续结果](hourly-dispatch.md#保存接续并结束)处理 |
| 已排空的人工单批、暂停或停止收尾，或当前没有后续小时接续授权 | `bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" finish --run-id "${OA_RUN_ID:?}" --require-drained` |
| 未知提交或用户要求立即停止，保留未完成任务并关闭 owner | `bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" finish --run-id "${OA_RUN_ID:?}"`，报告未完成范围，不声称批次完成 |

`--require-drained` 返回 `run_not_drained` 时保留 owner，按实际 `blockers` 接管未完成审核或只读回查。不能因拒绝而改用无保护参数重试。正常收尾在 owner 工具会话退出前保持当前主对话。

关闭前后均使用同一查询入口，分别核对验收事实及关闭结果：

```bash
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" status --run-id "${OA_RUN_ID:?}" --view batch
```

`finish` 等待当前采集与 Writer 完成并落盘，再关闭本轮、socket 并释放 `approval.lock`；正常批次先完成逐单验收再调用它。保留关闭回执，等待原 owner 工具会话退出，最后确认指定轮次 `run.status=closed`、`owner.reachable=false`；整批完成还须 `closeout.complete=true`。提前轮换或停止排空时核对 `closeout.drained=true` 并说明未领取数。`finish` 返回 `closed` 只证明轮次关闭。使用上述公开查询收尾，无需临时读库。EOF 同样触发安全关闭；采集或 Writer 客户端断开仍完成并保存原操作；采集进程异常登记技术失败，Writer 异常退出保留已保存交付，已有 started 尝试转为 unknown 并阻断。进程收到 SIGTERM/SIGHUP 时同样等待两者收尾；强制终止不清除未结束回执，不重发。
