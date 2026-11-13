# OA 能力地图

维护 OA 用户可见的 CLI 能力、入口和可观察完成条件。先选择对应页面，再驱动所列入口；不要用一条方便路径代替整项覆盖。

| 能力 | 用户可以完成什么 | 本地可验范围 | 真实边界 |
| --- | --- | --- | --- |
| [批次与审核](approvals.md) | 看队列、领取、冻结、预览、提交、回查、关闭及接续 | 三流程 demo 和现有 CLI harness | OA、商旅、发票、BI/SAP/DMS；真实写入另核本次授权 |
| [本地材料](local-materials.md) | 文件/清单准备、原文与概要读取 | 合成 PDF、既有格式夹具 | 本地来源不等于官方来源 |
| [识别与阅读副本](recognition.md) | 识别、进度、定向复读、压缩 | 压缩与报告进度；识别回归用边界 fake | 实际模型有独立调用与来源证据；包装走目视规则 |
| [流程预测](prediction.md) | 查看完整节点、办理人及财务前置标记 | 既有合成接口回归 | OA 原生 API，可能生成或刷新预测缓存 |
| [参考报价](references.md) | 导入 TPM 指导价供采集冻结 | 独立 root 的合成工作簿 | 正式报价来源及业务有效性须单独确认 |

## Baseline preconditions

- 从当前 checkout 的公开 CLI 驱动，使用仓库锁定的 Python 环境。
- 本地证据与 demo root 每次新建；`/Users/synear/Documents/OA/runtime/hourly-preaudit` 是业务 runtime，不能用作验证 scratch。
- 正式主线与真实启动要求沿用 [AGENTS.md](../../../../AGENTS.md) 和 [SECURITY.md](../../../../SECURITY.md)。本技能不增加真实写入授权。
- 三个模块直达入口 `apps.oa.approvals`、`apps.oa.materials`、`apps.oa.references` 仍存在；统一入口分别为 `apps.oa approvals/materials/references`。

## Proof and skip reporting

每次证据写明 feature ID、入口、模式、checkout/HEAD、命令与结果。列出通过、失败、未运行；只读查询不代替真实写入，demo 不代替业务审核。静态源码、单测、公开 CLI 运行和真实外部验收分别表述。

本次生成技能必须至少实际运行一项地图中的安全能力并执行 cleanup，随后回读证据。其余能力仅因有文档或已有测试而不自动计为本次通过。
