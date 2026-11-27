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

完整[能力地图与验证入口](.cursor/skills/verify-oa/features/README.md)按用户路径组织；[验证技能](.cursor/skills/verify-oa/SKILL.md)提供隔离演示、证据和清理步骤。

## 流程智能预测

无需启动审批轮次，直接查询 OA 原生的后续流转预测：

```bash
bash .codex/python.sh -m apps.oa approvals predict --request-id "$OA_REQUEST_ID"
bash .codex/python.sh -m apps.oa approvals predict --request-id "$OA_REQUEST_ID" --format json
```

使用私有配置中既有应用的 OA API 认证，核对当前账号后，返回全部节点及办理人的姓名和 ID，区分已经过、当前、预测和未知状态。JSON 同时保留完整原始响应，字段含义见[接口合同](docs/agents/oa-api.md#流程智能预测)。无需浏览器、Cookie 或页面操作；不另行注册应用。预测可能生成或更新 OA 的预测缓存，不提交审批或保存表单。等待时限和重新预测参数见 `predict --help`。

财务前置判断读取 OA 私有认证文件同目录的 `prediction-finance-precheck.json`，使用指定审批人和固定发起人名单。`predict` 只输出命中原因及建议处理动作，不写审批。纳入财务前置规则的报销预审在显式授权写入时重新预测，命中后携带签字意见转办给后续首个人工财务节点的唯一办理人；路径或人员不明确时暂停。适用流程、配置格式及转办验收边界见[接口合同](docs/agents/oa-api.md#财务前置标记)。真实名单保留在私有配置，不提交源码仓库。

## 审批运行

开发分支可启动合成轮次：

```bash
export OA_RUNTIME_ROOT="$PWD/runtime/demo"
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" start --demo
```

start 是持有锁和 socket 的前台 owner。保持原工具会话的标准输入打开，例如使用 `exec_command` 的 `tty=true`；另一个终端使用同一绝对 root 调用其他操作。正常收尾通过 finish，等待采集、Writer 落盘和 owner 退出。失联轮次的显式核对和关闭见[连续处理规则](.agents/skills/oa-finance-preaudit/references/hourly-dispatch.md#进度与失联核对)。

```bash
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" status --view batch
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" show --task-id "$OA_TASK_ID"
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" collect \
  --expected-task-id "$OA_TASK_ID"
```

TPM 单据经本次明确授权跳过一份损坏附件时，见 [TPM 单附件例外](.agents/skills/oa-finance-preaudit/references/flows/tpm-small-marketing-fee.md#单附件例外)。

完成本流程适用的材料识别后派发审核。`OA_RECOGNITION_FILE` 使用完整识别报告；无适用报告时省略该选项。

```bash
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" dispatch-review \
  --expected-task-id "$OA_TASK_ID" --agent-id "$OA_REVIEW_AGENT_ID" \
  --agent-workflow-key "$OA_WORKFLOW_KEY" --recognition-file "$OA_RECOGNITION_FILE"
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" deliver \
  --task-id "$OA_TASK_ID" --agent-id "$OA_REVIEW_AGENT_ID" \
  --material-file "$OA_MATERIAL_FILE" --material-sha256 "$OA_MATERIAL_SHA256" \
  --opinion-file /absolute/path/opinion.txt --review-file /absolute/path/review.json
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" preview --task-id "$OA_TASK_ID"
bash .codex/python.sh -m apps.oa.approvals --root "${OA_RUNTIME_ROOT:?}" finish --run-id "$OA_RUN_ID" --require-drained
```

变量取本次工具返回的实际 Agent、任务、流程和轮次身份。真实 start 仅允许干净、已发布且与 origin/main 一致的 main，启动前 fetch 核对；运行中的 checkout 不自动更新。真实运行将 root 设为本项目 runtime 的绝对路径。默认目录不受启动 cwd 影响，锁与账本按所选 root 协调。

`deliver` 必须提供 `--review-file`，记录本次实际审核完成状态、材料版本及证据，保存成功才释放审核槽。`submit` 与 `preview` 只消费已保存结果；格式和失败分支见[提交前审核结果](.agents/skills/oa-finance-preaudit/references/review-assessment.md)。提前关闭的原因通过 `finish --stop-file` 保存，最终汇报读取 `status --view batch` 的 `execution`，不能只依据提交回执判断整轮正常。材料保留见下文。

新轮默认排除同一次投递的历史预览，以及已生成意见但没有提交尝试的排队或待提交任务，保留原意见等待明确授权；新的投递身份仍可入队。提交默认只写本地预览；真实写入需要当前授权及 `--confirm-write YES`，海外还需独立的 `--allow-overseas YES`。预览、失败登记和回查的本地写入与外部写入分别由 help 标明。结果未知只回查，不自动重发。轮次结束不表示所有外部业务已完成。有积压的独立对话接续使用 `finish --require-drained --handoff` 返回的信息与 `start --continue-from <前轮ID>`，执行约定见[连续处理规则](.agents/skills/oa-finance-preaudit/references/hourly-dispatch.md)。

明确授权重审已有预览或未提交意见时，使用 `start --request-id <申请ID> --retry-reviewed YES`，重新领取、采集完整材料并审核；旧意见保留。该参数不继承到后续接续，也不授权真实写入。历史失败的候选查询、授权恢复及接受时间排序见[失败待办恢复](.agents/skills/oa-finance-preaudit/references/hourly-dispatch.md#采集停滞与历史失败)。

批次范围、FIFO、在途数量、Agent 轮换和审核规则统一见[财务预审 Skill](.agents/skills/oa-finance-preaudit/SKILL.md)。海外规则见[海外 Skill](.agents/skills/oa-overseas-invoice/SKILL.md)。Python 不替 Agent 形成财务业务结论。

经销商关户使用`dealer_closure`进入同一批次与提交入口，按[关户 Skill](.agents/skills/oa-dealer-closure/SKILL.md)核对本轮冻结BI、正式SAP、DMS及表单，输出✅/❗简版意见。正常审批与未阅转发批注分别核对提交上下文，后者反馈意见并核验原流程未推进；已阅记录不重复列入。私有连接配置见Skill数据入口说明。

经销商临时退款和调账使用 `dealer_temporary_refund`、`dealer_balance_adjustment`。在 OA 私有认证文件同目录的 `dealer-finance.json` 配置 `refund_dms_connector`（权限 600 的 DMS 只读模块绝对路径）、`adjustment_groups`（两组已确认的客户编码）及 `adjustment_file_origin`（经核验的 DMS 签署件来源）。需要按凭证确认调账账务归属时，可在 `adjustment_company_by_request` 按申请及调出方绑定确认公司。正常审批仅提交对应 RPA 预审节点的意见。未阅转发只反馈批注，使用转发身份和原节点回查。

## 材料与参考资料

商旅采集默认使用原 Chromium 与 RPA 会话。可在启动进程时设置 `OA_TRAVEL_BROWSER_ENGINE=moli`、`OA_TRAVEL_SESSION_SOURCE=api` 和 `OA_MOLI_BIN=/absolute/path/moli`，启用独立只读会话及 Moli。`chromium` 加 `api` 使用同一只读会话运行无头 Chromium；`moli` 加 `rpa` 仍需现有 RPA 登录态。浏览器读取授权仍由 `OA_ALLOW_BROWSER_DOWNLOAD` 控制。API 模式复用现有注册凭据，不注册新应用，必须通过账号核对才继续。

可选路径仅用于商旅正文、酒店明细和关联附件，不进入审批提交、海外回填或其他流程的下载路径。Moli 兼容故障在共享读取期限内最多回退一次无头 Chromium；身份、权限、源数据或必需请求被拦截时失败。进入下载批次后固定已选引擎，下载失败不会切换引擎。Cookie 只在单据会话内存中使用，回环代理和临时证书随会话关闭，不修改系统信任、全局代理或个人浏览器。

当前验收的 Moli 为 macOS ARM64 的 1.1.19，二进制 SHA-256 为 `ac6e4bf3acd4ccc2fb0fefa305b32a935e93ab1a5302ed1983b7020d6bf65b25`。其他构建在补充兼容验收前按引擎不可用处理并记录 Chromium 兜底。程序不会下载或升级 Moli。页面兼容补丁只匹配已验收脚本摘要；未知脚本版本只允许无补丁 Chromium 尝试，并须通过相同材料校验。

```bash
bash .codex/python.sh -m apps.oa.materials prepare --help
bash .codex/python.sh -m apps.oa.materials read --help
bash .codex/python.sh -m apps.oa.materials recognize --help
bash .codex/python.sh -m apps.oa.materials compress --help
bash .codex/python.sh -m apps.oa.references import-tpm --help
```

读取冻结材料的规模与原文位置可用 `read /absolute/path/material.json --view outline --format json`。默认 `read` 仍返回完整正文。概要列出字段数、由字段路径记录的各明细表行数、顶层资源数、含归档子资源的逻辑资源总数、归档子资源数、准备图片数和结构化内容数；相同内容与共享准备路径单独列示，不减少逻辑计数。JSON Pointer 指向冻结文件，`text_section` 与 `text_index` 对应完整读取中的节名和零基数组索引。概要不计入已读覆盖，审核仍须完整读取冻结材料和规则。

本地 prepare 保留原件、资源和父级归属，使用新输出目录；本地来源不构成已核实的 OA 来源。识别可能调用模型，调用前按当前授权选择材料与范围。阅读副本不能代替原件。

读取 RAR5 分卷时，将同一来源的 `.partN.rar` 原件一起交给材料准备。程序核对卷号、卷尾和跨卷文件衔接，再用 PATH 中的 `7zz` 校验和解包。支持用一个跨越所有卷的数据成员的校验值核验完整卷组。含多个数据成员的卷组缺少可验证的整组关联，保留原件和失败诊断。每卷保留原件及出处，展开项可回查完整卷组。通过验证的卷组按单成员 160 MiB、总解包 200 MiB 限制读取。嵌套归档及它展开的内容都计入总量，并受同一解包时限约束。macOS 可使用 `brew install sevenzip`，也可将官方 26.06 发行版的 `7zz` 放入 PATH。 [7-Zip 26.06](https://github.com/ip7z/7zip/releases/tag/26.06)

当前节点提交并回查确认后，账本保留实际提交的意见、最终审核版本的待核事项、回执及查询和防重所需的标识。本地附件原件、历次采集材料、解析文件及库内材料正文随该单清理。按规则留有业务待核项，以及曾失败后重试成功的单据，同样按最终回执处理。未提交、预览、结果不明以及未完成任务共用的材料保留。清理失败或遇到文件仍在使用时，保留回执和待清理标记，在启动或收尾时补扫。新节点重新采集、审核。

历史资料默认只读预览，使用 `bash .codex/python.sh -m apps.oa.approvals --root /absolute/runtime/root cleanup`，结果含逐单回执标识、候选路径和文件分配量。历史清理获授权且当前 owner 退出后才追加 `--execute`。此命令处理指定 root 的已核实单据。独立分析和范围外文件需要单独核对。数据库精简后可在维护窗口压缩 SQLite，日常处理会复用已释放页。账本到期检查只处理已经清理、超过 365 天且符合原有核验和时限条件的记录。

附件缓存默认同 root，显式 `OA_FINANCE_PREAUDIT_CACHE_DB` 可共享或禁用。凭据保存在进程环境或工作树外的私有配置中。

- [附件识别要求](docs/agents/attachment-recognition.md)
- [图片压缩要求](docs/agents/image-compression.md)
- [发票原件要求](docs/agents/invoice-originals.md)
- [OA 接口合同](docs/agents/oa-api.md)
- [海外回填规则](docs/overseas-invoice.md)

参考报价导入后再采集 TPM 材料；未配置时材料明确记录缺口。完整测试、合成运行、真实模型和真实业务验收分别报告。没有独立授权时不调用真实业务接口。

商旅账号或公司配置缺失的已授权交接，按[商旅 SSO 受限交接](.agents/skills/oa-finance-preaudit/references/travel-sso-handoff.md)配置私有授权。公开 `collect` 自动按单绑定，不需要临时包装器补参数；授权缺失时保持普通采集。完整流程回归入口为 `bash .codex/python.sh -m pytest -q tests/approvals/test_travel_sso_policy.py`。
