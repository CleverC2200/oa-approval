# 本地材料准备与读取

将本地文件或带资源归属的清单准备为材料包，完整读取正文，或用概要定位规模和原文位置。

## Sub-features

- `prepare-file` 从一个或多个 `--file` 生成新材料目录。
- `prepare-manifest` 从 `--manifest` 保留资源 ID、父子关系及归档资源。
- `read-full` 以 text 或 JSON 返回完整材料正文。
- `read-outline` 返回规模与 JSON Pointer，供定位后继续读完整材料。

## How to get to it (user POV)

- `bash .codex/python.sh -m apps.oa materials prepare --help`
- `bash .codex/python.sh -m apps.oa.materials prepare --help`
- `bash .codex/python.sh -m apps.oa materials read --help`
- `bash .codex/python.sh -m apps.oa.materials read --help`

## Driving it with OA CLI

Preconditions:

- Launch 中的 CLI doctor 通过，输出 evidence 目录尚不存在。
- 使用脚本生成的合成 PDF，不以业务原件作可删除夹具。

- **文件入口。** 运行 `.cursor/skills/verify-oa/scripts/local-materials.sh "$OA_VERIFY_EVIDENCE"`。脚本调用统一入口 `materials prepare --file`，要求 complete、1个资源、1页图片。
- **清单入口。** 同一脚本调用模块入口 `prepare --manifest`，清单为相同 PDF 的两个独立资源。要求2个资源、2页图片，ID仍分别为 `same-a`、`same-b`。
- **完整读取。** 脚本分别执行统一入口 `read --format json` 和模块入口默认 text；两份正文都包含合成标记 `OA verification fixture 12.50`。
- **概要读取。** 脚本执行 `read --view outline --format json`；结果为结构化 outline，准备记录保持 complete，概要不含合成正文。
- **原件与副作用。** `proof.json` 记录原件前后 SHA-256一致、资源 ID及 Git refs 未变化。`commands.txt` 与逐步 stdout/stderr/退出码记录动作和结果；`cleanup.json` 确认 scratch 已删除。

## Gotchas

- prepare 要求新的输出目录，拒绝已存在目录；`--file` 与 `--manifest` 互斥。
- 概要不计已读覆盖。压缩、格式转换只生成副本，资源身份不能被相同内容合并。
- 此演示覆盖单页 PDF 和清单资源身份；其他格式、损坏文件、归档边界按对应既有回归或新输入另验。
- 本地包不能直接替代 collect 的官方冻结材料用于真实提交。
