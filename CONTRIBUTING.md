# 本地开发

开发在当前项目或其受管工作树，分支使用 `codex/`，旧克隆保持只读。操作入口见 [README](README.md)，有效业务与授权约定见 [AGENTS.md](AGENTS.md)。

通过 `.codex/python.sh` 复用锁定的 Python 3.11 环境。实现和测试按 approvals、materials、references 的能力归属就近修改；共享准备放在实际使用它的测试组附近。SQLite 和临时文件使用真实实现，外部传输使用明确 fake。

优先运行受影响测试，整合后运行公开合成 CLI 演示和 `bash .codex/python.sh check`。该检查与提交钩子和 CI 共用，任一阶段失败均返回失败；检查输入须与暂存内容一致。既有自定义 hooksPath 保留。

业务规则只维护在已有 Skill 和流程规则中。原件、页码、字段与资源关系保持可回查。修改业务规则时用同份材料对照原意，按[结果合同](.agents/skills/oa-finance-preaudit/references/result-contract.md#审核依据与对外意见)检查关键信息。

本地提交采用英文 Conventional Commits，记录原因与有效验证。推送、PR、合并、发布和真实服务验收遵循当前用户授权。
