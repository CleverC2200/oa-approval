# OA 能力 CLI

独立项目位于 `/Users/synear/Documents/OA`，代码仓库为 [oa-approval](https://github.com/CleverC2200/oa-approval)。正式主线与真实审批启动要求见 [AGENTS.md](AGENTS.md)，共享材料边界见 [SECURITY.md](SECURITY.md)。

## 开发与离线验证

通过 Codex 的“准备共享 Python 依赖” Action 或以下命令准备锁定的 Python 3.11 环境。已有兼容共享环境直接复用。

```bash
bash .codex/python.sh setup
bash .codex/python.sh -m pytest -q tests/approvals/test_cli.py::test_demo_runs_collect_preview_submit_and_postcheck
bash .codex/python.sh check
```

合成演示通过公开 CLI 完成领取、材料、识别来源保存、预览、fake 提交、回查和结束。SQLite 与文件使用真实实现，外部传输为明确 fake，不访问真实服务。离线通过不代表业务验收。

## 能力入口

```bash
bash .codex/python.sh -m apps.oa --help
bash .codex/python.sh -m apps.oa.approvals --help
bash .codex/python.sh -m apps.oa.materials --help
bash .codex/python.sh -m apps.oa.references --help
```

审批负责轮次、任务、材料冻结、预览、提交与回查；材料负责完整准备、读取、识别和阅读副本；参考资料负责当前 TPM 报价导入。具体参数、副作用和结果见各操作 `--help`。能力内部直接组合函数，正常使用无需临时 Python 或 SQL。

## 审批运行

开发分支可启动合成轮次：

```bash
export OA_RUNTIME_ROOT="$PWD/runtime/demo"
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" start --demo
```

start 是持有锁和 socket 的前台 owner。保持原工具会话的标准输入打开，例如使用 `exec_command` 的 `tty=true`；另一个终端使用同一绝对 root 调用其他操作。正常收尾通过 finish，等待采集落盘和 owner 退出。

```bash
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" status
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" show --task-id "$OA_TASK_ID"
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" collect \
  --agent-id "$OA_REVIEW_AGENT_ID" \
  --expected-task-id "$OA_TASK_ID" --agent-workflow-key "$OA_WORKFLOW_KEY"
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" preview --task-id "$OA_TASK_ID" --opinion-file /absolute/path/opinion.txt
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" finish --run-id "$OA_RUN_ID"
```

变量取本次工具返回的实际 Agent、任务、流程和轮次身份。真实 start 仅允许干净、已发布且与 origin/main 一致的 main，启动前 fetch 核对；运行中的 checkout 不自动更新。真实运行将 root 设为本项目 runtime 的绝对路径。默认目录不受启动 cwd 影响，锁与账本按所选 root 协调。

提交默认只写本地预览；真实写入需要当前授权及 `--confirm-write YES`，海外还需独立的 `--allow-overseas YES`。预览、失败登记和回查的本地写入与外部写入分别由 help 标明。结果未知只回查，不自动重发。轮次结束不表示所有外部业务已完成。

批次范围、FIFO、在途数量、Agent 轮换和审核规则统一见[财务预审 Skill](.agents/skills/oa-finance-preaudit/SKILL.md)。海外规则见[海外 Skill](.agents/skills/oa-overseas-invoice/SKILL.md)。Python 不替 Agent 形成财务业务结论。

## 材料与参考资料

```bash
bash .codex/python.sh -m apps.oa.materials prepare --help
bash .codex/python.sh -m apps.oa.materials read --help
bash .codex/python.sh -m apps.oa.materials recognize --help
bash .codex/python.sh -m apps.oa.materials compress --help
bash .codex/python.sh -m apps.oa.references import-tpm --help
```

本地 prepare 保留原件、资源和父级归属，使用新输出目录；本地来源不构成已核实的 OA 来源。识别可能调用模型，调用前按当前授权选择材料与范围。阅读副本不能代替原件。

运行数据与审批账本继续使用所选 root 下的原有布局。原件、历史冻结材料、意见和回执保留。附件缓存默认同 root，显式 `OA_FINANCE_PREAUDIT_CACHE_DB` 可共享或禁用；资源槽沿用现有协调目录约定。凭据保存在进程环境或工作树外的私有配置中。

- [附件识别要求](docs/agents/attachment-recognition.md)
- [图片压缩要求](docs/agents/image-compression.md)
- [发票原件要求](docs/agents/invoice-originals.md)
- [OA 接口合同](docs/agents/oa-api.md)
- [海外回填规则](docs/overseas-invoice.md)

参考报价导入后再采集 TPM 材料；未配置时材料明确记录缺口。完整测试、合成运行、真实模型和真实业务验收分别报告。没有独立授权时不调用真实业务接口。
