# 批次、审核与提交

从待办建立轮次，冻结本单材料，保存审核意见并按授权提交，使用公开回执确认结果后关闭或保存接续。

## Sub-features

- `queue`：`start` / `pending`、`status`、FIFO、批次边界与共享互斥。
- `review`：`collect`、`show`、`preview` 和规则/材料版本绑定。
- `write`：`submit`、防重发、三项回执及海外字段一致性。
- `resolve`：`postcheck`、`fail`、显式历史重试与失联核对。
- `close`：`finish --require-drained`、handoff、合法接续和保留材料的 cleanup。

## How to get to it (user POV)

- 统一入口 `bash .codex/python.sh -m apps.oa approvals --help`。
- 模块入口 `bash .codex/python.sh -m apps.oa.approvals --help`。
- 财务六类：差旅、招待、业务支持、小额市场、TPM小额市场、通用费用；另有经销商关户、包装版面、海外发票。对应规则入口为 [财务路由](../../../../.agents/skills/oa-finance-preaudit/SKILL.md)、[关户](../../../../.agents/skills/oa-dealer-closure/SKILL.md)、[海外](../../../../.agents/skills/oa-overseas-invoice/SKILL.md)。能力存在不扩大本次授权范围。

## Driving it with OA CLI

Preconditions:

- demo 使用本次创建的独立 root；真实执行先读取现有业务 Skill，使用其主线、身份、互斥及授权约束。

- **启动 demo。** 在原生命令工具中以 `tty=true` 运行以下命令，保存返回的绝对 root 和原会话ID，保持 stdin 打开：

  ```bash
  OA_VERIFY_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/oa-demo.XXXXXX")"
  printf 'OA_VERIFY_ROOT=%s\n' "$OA_VERIFY_ROOT"
  bash .codex/python.sh -m apps.oa approvals --root "$OA_VERIFY_ROOT" start --demo --full
  ```

- **只读核对。** 其他命令调用使用保存的同一绝对 root。运行 `status --view batch`，要求 run_id 匹配、owner可达；`show --task-id` 的 ID取本次 start/collect 返回值。
- **领取。** 执行 `collect --agent-id demo-reviewer --expected-task-id "$OA_TASK_ID" --agent-workflow-key "$OA_WORKFLOW_KEY"`。两个变量取本次队列首单，要求返回本次 material_file，文件存在且 prepared 含合成页。
- **预览。** 将“合成验证意见，不是业务审核结论”保存为意见文件，运行 `preview --task-id "$OA_TASK_ID" --opinion-file "$OA_OPINION_FILE"`。要求 preview；再 show，submissions 仍为空，不能只看退出码。
- **fake 写入。** 按[审核结果接口](../../../../.agents/skills/oa-finance-preaudit/references/review-assessment.md)记录本次合成材料的实际验收，保存 `$OA_REVIEW_FILE`。仅对本次 `demo=true` owner运行 `submit --task-id "$OA_TASK_ID" --opinion-file "$OA_OPINION_FILE" --review-file "$OA_REVIEW_FILE" --confirm-write YES`，核三项 true。再 `postcheck` 和 show，仍只有一次提交。不能把相同命令应用到真实 root。
- **全链路 harness。** 运行 SKILL 的既有 `test_demo_runs_collect_preview_submit_and_postcheck`。它覆盖三类合成流程、保存识别来源、预览不产生提交、fake 提交与回查；模型响应在传输边界 fake。
- **异常分支。** 使用 `bash .codex/python.sh scripts/verify-review-outcomes.py --root <新的私有合成目录>`，通过真实公开 CLI 的 demo owner 验证业务待核后补位、拒绝假读取失败停止、失败登记及证据保留、异常标题和归档资格。`proof.json` 保存验收，既有审批数据库会被拒绝；该脚本不验证真实图片识别准确性。
- **收尾。** 所有已领取任务完成后，`finish --run-id "$OA_RUN_ID" --require-drained`，变量取启动回执。等待原会话退出，status核 closed、owner不可达及已处理任务的 verified 回执。部分未领取不代表队列耗尽。

## Gotchas

- `pending` 也是启动 owner，不是无副作用队列列表。`preview` 写本地审核；`postcheck` 可补写本地回执，均不能按名称推断无写入。
- 真实 start 会读取待办并写账本；真实 submit 和转办有外部业务副作用。海外另需限定字段计划及 `--allow-overseas YES`，成功还须 fields_match。
- 关户的批注反馈与正常审批分别验收；包装仅目视文字核对。demo 不覆盖这三类专用业务。
- unknown/submitting 不重发。失联关闭、指定失败/预览重试、人工确认未写入均按既有规则和当前明确授权，不作为通用验收默认步骤。
- `cleanup` 默认预览，`--execute` 会删除符合保留条件的材料。这里只清理自己创建的 demo scratch，不清理业务 runtime。
