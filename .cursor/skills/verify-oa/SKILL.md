---
name: verify-oa
description: 梳理和验证 OA 的公开 CLI 能力，覆盖批次审核、材料处理、流程预测和报价导入。用于能力导航、变更验收及复现；从本地材料或隔离 demo 开始，真实服务按本次授权验证。
---

# OA 能力验证

先读[能力地图](features/README.md)，选择本次改动涉及的入口。主操作面是 `python -m apps.oa`，也保留三个模块直达入口。商旅浏览器属于取数适配器；操作步骤和权限沿用既有业务 Skill。

## Launch

在目标 checkout 根目录执行。复用 `.codex/python.sh` 的共享 Python 3.11 环境；环境缺失时才执行仓库的 `bash .codex/python.sh setup`。

```bash
bash .codex/python.sh -m apps.oa --help
```

本地材料能力是短命令，无常驻服务。可复现的合成材料验证：

```bash
OA_VERIFY_EVIDENCE="/Users/synear/Documents/OA/runtime/verification/materials-$(date +%Y%m%dT%H%M%S)"
.cursor/skills/verify-oa/scripts/local-materials.sh "$OA_VERIFY_EVIDENCE"
```

输出目录必须尚不存在。脚本返回 `status=passed`，随后 `cleanup.json` 记录临时目录删除成功，才完成本地验证。

批次功能使用现有 demo：创建独立临时 root，在原生命令工具中以 `tty=true` 启动并保留标准输入；具体命令见[批次与审核](features/approvals.md)。`start` 首行 JSON 的 `demo=true`、实际 `run_id` 和任务清单表示就绪。用同一 root 的 `finish` 收尾并等待原会话退出。

demo 只锁自己的 root，可与其他隔离 demo 并行。真实审批使用项目共享锁和固定业务 runtime；不启动第二个真实实例，不改运行中 checkout。此技能不启动小时任务、不消费现有接续，也不修改聊天归档或调度配置。

## Doctor

先执行上面的 `apps.oa --help`，确认解释器和公开入口可用。已有批次用只读入口核对自己持有的实例：

```bash
bash .codex/python.sh -m apps.oa approvals --root "$OA_VERIFY_ROOT" status --view batch
```

`OA_VERIFY_ROOT` 必须取本次启动保存的绝对路径。核对返回的 run_id 与首行一致、运行中的 owner 可达；不匹配便停止驱动。`not_initialized` 不会创建账本。该检查不刷新 OA，也不证明真实服务的认证仍有效；不要用真实 `predict` 充当无副作用健康检查。

## Drive

| 目标 | 地图与验证入口 |
| --- | --- |
| 批次、材料冻结、意见预览、提交与回查 | [批次与审核](features/approvals.md)，隔离 demo / 已有真实轮次的只读查询 |
| 本地文件与清单准备，完整正文和概要读取 | [本地材料](features/local-materials.md)，`local-materials.sh` |
| 附件识别、进度、定向复读和压缩副本 | [识别与阅读副本](features/recognition.md) |
| 后续节点预测、财务前置标记 | [流程预测](features/prediction.md) |
| TPM 报价导入与采集时冻结 | [参考报价](features/references.md) |

每项注明本次实际驱动的入口与未验入口。demo 仅提供差旅、招待、通用费用三种合成流程；真实业务审核的九类流程与授权边界见地图，不把 demo 通过当作九类业务都已验收。

## Evidence

证据存入本次独立的 `runtime/verification/` 子目录，不进入 Git。保留命令、stdout、stderr、退出码以及操作前后的公开状态；材料验证保留原合成输入、准备结果、完整读取与概要结果。

- 本地脚本验证两个 prepare 入口、两种 read 展示、资源身份、原件完整性及 Git refs 未变化。脚本不声称验证真实 OA、模型识别、所有文件格式或外部网络行为。
- preview 写本地审核记录。用公开 `show` 验证没有提交尝试，再以 batch status 验证队列变化；不能把“预览”理解成完全不写文件。
- demo 的 collector、writer、predictor 在既有生产接口边界注入合成实现；SQLite、材料准备和 CLI 路径是真实实现。真实识别和真实业务回执必须另验，不能用内部 setter 造出成功状态。
- 真实财务及包装成功须核 `submit_ok`、`done_hit`、`postcheck_done_hit`；海外还须 `fields_match`。未知提交只走 `postcheck`，不重发。

## Cleanup

本地脚本在成功和失败时都只删除自己创建的临时 PDF 工作目录，`cleanup.json` 与全部证据保留。结束后回读 `proof.json` 和 `cleanup.json`，确认输入、输出和记录仍在。

demo 正常完成使用 `finish --require-drained`，核对 closed、owner 不可达与原工具会话退出。失败迭代先尝试正常 finish；仅能终止自己启动并记录了身份的演示进程，不按进程名杀进程。删除独立 scratch root 前将需要的材料、回执和日志复制到 evidence；证据目录不在删除范围内。真实 runtime、业务附件、账本及原始失败证据不属于这里的清理对象。

## Helpers

唯一新增脚本是 `scripts/local-materials.sh`，调用方式见 Launch。它经公开 CLI 驱动本地 PDF 准备和读取，不调用审批入口。批次自动回归复用已有 harness：

```bash
bash .codex/python.sh -m pytest -q tests/approvals/test_cli.py::test_demo_runs_collect_preview_submit_and_postcheck
```

该测试的识别响应也是明确 fake；测试通过与真实模型、真实服务验收分别记录。能力变化后用 `/maintain-verification-skill` 更新地图与实际证据。
