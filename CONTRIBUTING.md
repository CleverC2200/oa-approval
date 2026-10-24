# 本地开发

本目录是独立的 OA 最小审批改造项目。开发在当前目录或其受管工作树，分支使用 `codex/`。旧克隆保持只读；真实 OA、真实模型调用和外部发布需要对应当前授权。

项目约定见 [AGENTS.md](AGENTS.md)，操作入口见 [README](README.md)。实现直接连接材料采集、SQLite 队列和 OA 提交。

## 按任务定位

首次开发先运行 [README 的合成 CLI 演示](README.md#离线开发入口)。下表给出改动入口和首选验证范围；跨能力改动按实际调用链补充受影响检查。

| 修改目标 | 实现入口 | 首选测试 |
| --- | --- | --- |
| 队列、领取与任务状态 | [Store](apps/oa/store.py)、[Approval](apps/oa/service.py) | [SQLite 队列](tests/serial/test_sqlite_store.py)、[审批执行](tests/serial/test_approval_service.py) |
| CLI、socket 与进程生命周期 | [CLI](apps/oa/cli.py) | [公开 CLI](tests/serial/test_cli.py) |
| 材料采集、来源与发票原件 | [材料组装](apps/oa_finance_preaudit/case_bundle.py)、[发票原件](apps/oa_approval/invoice_originals.py) | [采集合同](tests/finance_preaudit/test_collect_case_contract.py)、[发票原件](tests/finance_preaudit/test_invoice_original_bodies.py) |
| 附件准备、识别与定位 | [准备](apps/oa_approval/attachment_extractors.py)、[识别](apps/oa_approval/attachment_recognition.py)、[读取](apps/oa_finance_preaudit/read_material.py) | [材料工具](tests/materials/test_tools.py)、[识别与旋转](tests/materials/test_recognition_rotation.py) |
| 提交与未知结果回查 | [Approval](apps/oa/service.py)、[Writer](apps/oa_approval/writer_api.py) | [审批执行](tests/serial/test_approval_service.py)、[提交回查](tests/serial/test_writer_verification.py) |
| 运行目录、缓存及识别输出 | [Approval](apps/oa/service.py)、[附件缓存](apps/oa_approval/attachment_cache.py) | [运行目录](tests/materials/test_runtime_locations.py) |
| 审核业务规则 | [财务预审 Skill](.agents/skills/oa-finance-preaudit/SKILL.md)、[海外 Skill](.agents/skills/oa-overseas-invoice/SKILL.md) | [规则冻结](tests/serial/test_rule_packages.py)，当前流程的材料与意见对照 |

共享测试支撑放在实际使用它的测试组附近。pytest fixture 使用相邻 `conftest.py`；可直接构造的发票传输 fake 见 [invoice_fakes.py](tests/finance_preaudit/invoice_fakes.py)。新测试复用这些支撑，场景数据保留在各自测试中。

## 修改与验证

复用兼容的 Python 3.11 环境，通过 uv 执行；依赖版本来自 `requirements-dev.lock`。对采集、提交、队列和新 CLI 的公开接口做聚焦测试，外部传输显式替换，本地文件和 SQLite 使用真实实现。整合后运行完整保留测试及静态检查，区分离线通过、真实模型和生产业务验收。

现有 `.codex/python.sh setup` 同时启用本地提交钩子，保留已有自定义 hooksPath；代码、测试或检查配置变动在提交前调用 `.codex/python.sh check`，与 Codex 校验 Action 和 CI 共用入口。相关检查输入存在未暂存修改时先整理暂存范围，避免用工作区内容替代待提交内容。

业务规则只维护在现有流程规则和共享付款规则；Agent 使用最终意见文本，不新增固定业务结论。材料原件、页码、字段和资源关系保持可回查。修改意见规则时，审查人用同份材料对照修改前后意见，按[结果合同](.agents/skills/oa-finance-preaudit/references/result-contract.md#审核依据与对外意见)确认关键信息保留。

本地提交说明采用英文 Conventional Commits，记录改动原因及有效验证。推送、PR、合并和发布按用户授权执行。
