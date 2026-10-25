# Triage 标签映射

本表是工程技能角色与 GitHub 标签的映射来源。

| 角色 | GitHub 标签 | 含义 |
| --- | --- | --- |
| `bug` | `bug` | 已有行为故障 |
| `enhancement` | `enhancement` | 新能力或改进 |
| `needs-triage` | `needs-triage` | 等待维护者评估 |
| `needs-info` | `needs-info` | 等待报告者补充具体信息 |
| `ready-for-agent` | `ready-for-agent` | 规格完整，可交给 Agent 执行 |
| `ready-for-human` | `ready-for-human` | 需要人工实施或判断 |
| `wontfix` | `wontfix` | 不再安排实现，包括已存在的能力 |

每个已 triage 的议题恰有一个分类角色和一个状态角色，其他辅助标签可保留。调整角色时移除同组旧标签；状态冲突先交维护者确认。

用户可明确覆盖通常的状态转换。报告者补充 `needs-info` 所需信息后，回到 `needs-triage` 重新评估。

因已实现而关闭的议题注明实现依据；拒绝增强需求的历史按 triage 技能的 `.out-of-scope/` 约定维护。
