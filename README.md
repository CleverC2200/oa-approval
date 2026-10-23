# OA 最小 SQLite 审批

此独立项目位于 `/Users/synear/Documents/OA`。入口为 `apps.oa.cli`，使用 SQLite 保存队列、审核意见和提交事实，文件目录保存完整材料。旧开发、生产克隆保持只读。

代码仓库为 [oa-approval](https://github.com/CleverC2200/oa-approval)。正式主线、开发分支及审批启动前的核对要求统一见[项目约定](AGENTS.md)，提交材料边界见[安全约定](SECURITY.md)。

## 启动与操作

运行和验证统一使用 `.codex/python.sh`，复用 Python 3.11 共享环境。首次使用时在 Codex Local environment 执行一次“准备共享 Python 依赖” Action，按 `requirements-dev.lock` 准备依赖；其余工作树直接复用。真实 `pending` 只接受干净、已发布且与 `origin/main` 一致的 `main`，不满足时在访问 OA 和创建队列前停止。定时任务使用同一入口，demo 仍可在开发分支离线执行：

```bash
bash .codex/python.sh -m apps.oa.cli --root runtime pending --batch-size 50 --limit 50
```

`pending` 重新拉取 OA 待办，创建本轮 SQLite 队列并全程持有文件锁，保持前台运行。再次启动同一根目录会返回 `already_running`。默认根目录是本项目的 `runtime/`。本机 Unix socket 把以下操作送到该运行服务，所有 OA 提交由服务主循环串行执行。

启动输出包含 `run_id`、总数 `total`、按 `workflow_key` 分类的数量 `by_workflow`、展示数 `shown` 和截断标记 `truncated`。默认 `tasks` 只展示队列前 20 条，保留 `id`、`request_id`、`received_at`、`workflow_key`、`status` 和 `title`。`pending --limit 5` 调整展示数，`--limit 0` 不展示任务行；`pending --full` 返回全部待办的完整字段并忽略 `--limit`。这些展示参数不改变队列。

`--batch-size N` 独立限定本轮可领取的全局 FIFO 排队前缀，在 `runs` 中保存并返回 `batch.size` 与 `batch.task_ids`；范围外待办仍保留在完整队列和统计中。省略该参数保留完整队列领取行为。批次执行及领取保护统一见下文 Skill，示例数量不构成真实审批授权。

限定处理指定申请时，重复传入 `pending --request-id <申请ID>`。每轮仍重新拉取真实待办，只纳入当前待办中匹配的申请，并在该范围内按 FIFO 排队；已提交成功或结果未知的任务保持原状态，不能重新领取。

```bash
bash .codex/python.sh -m apps.oa.cli --root runtime collect \
  --agent-id "$OA_REVIEW_AGENT_ID" \
  --expected-task-id 1 --agent-workflow-key travel_expense
bash .codex/python.sh -m apps.oa.cli --root runtime submit --task-id 1 --opinion-file /absolute/path/opinion.txt
bash .codex/python.sh -m apps.oa.cli --root runtime submit --task-id 1 --opinion-file /absolute/path/opinion.txt --confirm-write YES
bash .codex/python.sh -m apps.oa.cli --root runtime postcheck --task-id 1
```

不传 `--confirm-write YES` 时只保存预览结果，不写 OA。海外回填使用 `--recognition-file`，真实提交还需独立的 `--allow-overseas YES`。实际授权以当前任务为准。

`OA_REVIEW_AGENT_ID` 填原生工具返回的实际 Agent 标识，预期任务和类型按本轮实际值填写。审核技术失败登记使用本地 `fail` 入口：

```bash
bash .codex/python.sh -m apps.oa.cli --root runtime fail --task-id 1 --reason 'native reviewer failed: concrete reason'
```

`fail` 由运行中的服务校验，只接受当前轮 `preparing`/`reviewing` 任务，不访问 OA。使用条件及批次收尾见下文 Skill。

socket `collect` 由唯一采集工作进程执行，原客户端等待最终结果时，服务主循环仍可受理其他单据的提交和回查。完整等待及补位方式见[Skill](.agents/skills/oa-finance-preaudit/SKILL.md#准备审核与补位)。

原 `pending` 进程也接受逐行 JSON，例如 `{"operation":"collect","agent_id":"<actual-native-agent-id>","expected_task_id":1,"agent_workflow_key":"travel_expense"}`，标准输入操作保持顺序执行。`{"operation":"finish"}` 或标准输入 EOF 先等待当前采集结束并落盘，再关闭本轮、socket 和 `approval.lock`。在 Agent 工具中启动长期入口时保持原工具会话的标准输入打开，例如使用 `exec_command` 的 `tty=true`，再用原 `write_stdin` 发送请求；不启动第二个队列进程。

## Agent 分工

批次范围、FIFO 领取、三单在途、同类多 Agent、补位、轮换及停止方式统一见[财务预审 Skill 的执行规则](.agents/skills/oa-finance-preaudit/SKILL.md#主-agent-执行规则)。Python 返回任务，不创建模型或原生审核 Agent。

审核子 Agent 根据 `collect` 返回的路径完整读取材料、匹配规则和全部识别内容，按同一 Skill 形成最终意见及附件观察。主 Agent 交换紧凑状态、数量和文件路径，保存意见并按授权提交。

## 数据与材料

`runtime/approval.sqlite3` 保存轮次、单据、待办、附件、识别内容、意见和提交。相同待办更新已有行，接收时间或可信待办实例变化才建立新待办。每轮仍重新拉待办，不续跑旧 Agent 或旧队列。

原件和准备页保存在本轮材料目录。保留完整表单、关联资料、ZIP/RAR 解包、PDF 全页、Office、Excel 和内嵌图片、XML/OFD、HEIC、图片压缩与 DeepSeek API 识别。压缩副本不代替原件，附件资源与明细归属分别保留。凭据使用进程环境或工作树外的私有 OA 配置，不提交数据库、附件或凭据。

TPM 终端形象制作费的参考报价仍使用本地导入入口，导入财务确认的工作簿后再取材料：

```bash
bash .codex/python.sh -m apps.oa_finance_preaudit.tpm_price_reference /absolute/path/reference.xlsx --runtime-root runtime
```

未导入时材料会明确记为 `not_configured`，由 Agent 按业务规则说明核验缺口。

- [DeepSeek 附件识别](docs/agents/attachment-recognition.md)
- [图片压缩](docs/agents/image-compression.md)
- [OA 接口合同](docs/agents/oa-api.md)
- [海外回填规则](docs/overseas-invoice.md)

## 本地演示与验证

```bash
bash .codex/python.sh -m apps.oa.cli --root runtime/demo pending --demo
```

该模式使用合成 OA 待办及合成 PDF，真实执行本地 SQLite、附件准备、队列和提交代码；OA 提交由本地 fake 完成，不访问网络、不调用模型。可在另一终端使用相同 `--root` 调用 `collect`、`submit`，或直接向原进程输入 JSON。演示意见须标明是合成测试内容，不能作为真实业务验收。

```bash
bash .codex/python.sh check
```

该入口供提交钩子、Codex 校验 Action 和 CI 共用，依次检查暂存文件的共享边界、Ruff 和完整离线测试；单项检查仍可使用：

```bash
bash .codex/python.sh -m pytest -q
bash .codex/python.sh -m ruff check apps tests
```

首次执行 `.codex/python.sh setup` 时启用本地提交钩子，已有自定义 hooksPath 保持原设置。Codex Local environment 保留“本地离线测试”和“静态检查”单项 Action，也可选“本地离线校验”；任一检查失败时停止并返回失败。

实现与改动原因以当前代码和 Git 提交记录为准。真实提交事实查询运行账本，离线验证不能代替业务验收。真实 DeepSeek 调用、海外字段回填及未覆盖流程仍需在独立授权后验收。
