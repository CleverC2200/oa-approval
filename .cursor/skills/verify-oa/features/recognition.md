# 附件识别与阅读副本

读取准备材料的附件正文，查看识别进度，对指定资源、页码、区域或旋转方向复读，并生成保留原件的压缩副本。

## Sub-features

- `recognize`：整份材料或文件识别，保存正文与来源。
- `reread`：`--resource-id`、`--page`、`--region`、`--rotate` 的定向选择。
- `progress`：已有报告的紧凑进度读取。
- `compress`：按边长、像素、字节预算生成阅读副本。

## How to get to it (user POV)

- `bash .codex/python.sh -m apps.oa materials recognize --help`
- `bash .codex/python.sh -m apps.oa.materials progress --help`
- `bash .codex/python.sh -m apps.oa materials compress --help`
- 两种 CLI 模块入口可互换，参数合同相同。

## Driving it with OA CLI

Preconditions:

- 压缩使用合成或已授权本地图片。真实模型调用需本次授权和现有模型凭据，正文留在私有 evidence。

- **识别。** 使用本次 prepare/collect 返回的 `OA_MATERIAL_FILE`，运行 `recognize --material-file "$OA_MATERIAL_FILE" --output-dir "$OA_RECOGNITION_DIR" --progress`；变量均为本次新路径。要求报告文件及每一适用资源的来源/页覆盖，不以 stdout 摘要代替正文。
- **复读。** 从本次材料取真实资源 ID，增加 `--resource-id "$OA_RESOURCE_ID" --page 1 --rotate 180`；检查报告 selection 与所选页对应，不能把复读视为整份覆盖。
- **进度。** `progress "$OA_REPORT_FILE"` 只读已有报告。记录命令前后报告内容一致，不触发新识别。
- **压缩。** `compress --file "$OA_IMAGE_FILE" --output-dir "$OA_COPY_DIR"`，检查 complete、输出能解码、预算满足、原件字节未变；小字不清仍回原图。
- **离线回归。** `bash .codex/python.sh -m pytest -q tests/materials/test_recognition_progress.py tests/materials/test_png_metadata.py`；只证明对应 fake/本地路径。

## Gotchas

- recognize 会调用模型，指定文件不等于完成来源核验，必须核报告状态和适用覆盖。
- 包装按既有中文图片目视 Skill，不能用财务识别、OCR或自动文字检测代替。
- `skipped` 只表示适用附件识别可跳过，不代表表单和业务审核完成。
- 本次本地材料证明没有执行真实 recognize；不能把准备页成功当作模型已读。
