# 本地开发

本目录是独立的 OA 最小审批改造项目。开发在当前目录或其受管工作树，分支使用 `codex/`。旧克隆保持只读；真实 OA、真实模型调用和外部发布需要对应当前授权。

项目约定见 [AGENTS.md](AGENTS.md)，操作入口见 [README](README.md)。实现直接连接材料采集、SQLite 队列和 OA 提交。

复用兼容的 Python 3.11 环境，通过 uv 执行；依赖版本来自 `requirements-dev.lock`。对采集、提交、队列和新 CLI 的公开接口做聚焦测试，外部传输显式替换，本地文件和 SQLite 使用真实实现。整合后运行完整保留测试及静态检查，区分离线通过、真实模型和生产业务验收。

现有 `.codex/python.sh setup` 同时启用本地提交钩子，保留已有自定义 hooksPath；代码、测试或检查配置变动在提交前调用 `.codex/python.sh check`，与 Codex 校验 Action 和 CI 共用入口。相关检查输入存在未暂存修改时先整理暂存范围，避免用工作区内容替代待提交内容。

业务规则只维护在现有流程规则和共享付款规则；Agent 使用最终意见文本，不新增固定业务结论。材料原件、页码、字段和资源关系保持可回查。修改意见规则时，审查人用同份材料对照修改前后意见，按[结果合同](.agents/skills/oa-finance-preaudit/references/result-contract.md#审核依据与对外意见)确认关键信息保留。

本地提交说明采用英文 Conventional Commits，记录改动原因及有效验证。推送、PR、合并和发布按用户授权执行。
