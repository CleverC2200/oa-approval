# TPM 参考报价导入

将经确认的报价工作簿导入指定 runtime，供适用 TPM 单据在 collect 时冻结，保留来源和价格列定位。

## Sub-features

- `import-tpm`：校验工作表、表头、材料编码和价格后导入。
- `freeze-reference`：适用单据 collect 时带入本次报价快照。
- `missing-reference`：未配置或不可读取时保留明确缺口。

## How to get to it (user POV)

- `bash .codex/python.sh -m apps.oa references --help`
- `bash .codex/python.sh -m apps.oa.references import-tpm --help`
- 正式报价来源由业务确认；导入不会证明报价仍然有效。

## Driving it with OA CLI

Preconditions:

- 仅向本次创建的隔离 root 导入合成工作簿；正式 root 的更新另核授权，避免改变后续真实审核依据。
- 工作表为 `参考报价单`，C:H 表头依次为材料名称、材料编码、材质说明/要求、单位、未税指导价（元）、含税指导价（元，1%）。

- **导入。** `bash .codex/python.sh -m apps.oa references --root "$OA_REFERENCE_ROOT" import-tpm "$OA_PRICE_WORKBOOK"`，变量取本次隔离 root 和合成输入。核返回路径、来源摘要、材料编码、source_row及H列价格，不只看输出文件存在。
- **冻结。** 获准的适用 TPM collect 完成后，公开材料中的 tpm_price_reference 与本次报价来源一致；没有配置时材料保留缺口，不补造参考价。
- **回归定位。** 查找 `tests/references/` 中 import-tpm / 工作簿行为测试，选择本次相关用例；测试不修改正式报价。

## Gotchas

- 只适用于终端形象制作费等当前规则明确使用报价的分支，不把有报价等同于费用合理。
- `--root` 与兼容参数 `--runtime-root` 互斥；默认 root 不用于演示导入。
- 报价导入写本地参考文件；材料已冻结后不以新报价替换本次冻结依据。
