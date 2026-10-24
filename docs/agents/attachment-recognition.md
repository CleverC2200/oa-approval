# 附件文字读取

图片通过现有 DeepSeek HTTP API 识别，报告标记 `source=deepseek`。凭据通过 `OA_REVIEW_API_KEY` 或现有私有 OA 配置读取，不放进命令参数、材料或 SQLite。

```bash
bash .codex/python.sh -m apps.oa_approval.attachment_recognition \
  --material-file "$OA_MATERIAL_FILE" \
  --output-dir /absolute/path/new-recognition-directory \
  --prompt '完整读取文字、数字、日期和印章；看不清的内容明确说明。'
```

`OA_MATERIAL_FILE` 使用本单 collect 返回的冻结材料路径。只有处理独立图片时才使用可重复的 `--file /absolute/path/page.png`；该输入使用临时资源编号，不能使用真实附件定向参数，保存观察前须对应到原附件。

读取 `collect` 返回的整份冻结材料时，使用 `--material-file /absolute/path/material.json`。`prepared.structured_contents` 的完整解析内容直接交给审核 Agent，报告标记 `source=parser`，保留原始文本、定位和子资源归属，不发送 DeepSeek。全部准备图片仍按现有流程识别，包括已有解析文字的 PDF 图片。只有文本的输入不需要 API key。该参数与单文件输入择一使用。

定向复读冻结材料时，从原完整识别报告选取目标 `part`，把 `report.parts[].resource_id` 传给 `--resource-id`，把同一项的 `page_number` 传给 `--page`。`prepared_resource_id` 标识准备子资源，不能用作该选择器；压缩包内页面仍使用报告中的父附件 `resource_id`。只传附件 ID 时读取该资源的图片和解析正文，加页码时只读选中图片页。工具先生成完整定位再筛选，保留原 `part_id`、`evidence_ref`、`prepared_resource_id`、页码和父附件归属。页码依赖附件 ID；无效参数或目标不存在时报错。

```bash
bash .codex/python.sh -m apps.oa_approval.attachment_recognition \
  --material-file /absolute/path/material.json \
  --resource-id attachment-1 --page 2 \
  --region 100 200 900 600 --rotate 90 \
  --output-dir /absolute/path/new-reread-directory
```

`--region x0 y0 x1 y1` 按原图像素坐标局部复读，选中内容必须是唯一图片页，文本不参与裁剪。定向报告的 `coverage` 分别为 `selected_resource`、`selected_page` 或 `image_region`；局部完成只表示选中内容已读，不能记为整份材料完成。默认仍读取整份材料。工具只读取并发送选中内容，原件、冻结材料和旧报告均保留。每次复读使用新的输出目录。

图片方向不正确时，使用 `--rotate 90`、`--rotate 180` 或 `--rotate 270` 顺时针旋转阅读副本，默认 `--rotate 0`。API 对应 `recognize(..., rotate=90)`。该选项适用于 `--file` 和 `--material-file` 的图片页，文本保持原样；原件和冻结材料均不覆盖。旋转副本只用于识别请求，报告中的 `rotate` 记录角度，`source_image_path` 指向旋转前的图片页，`resource_id`、`prepared_resource_id` 和 `page_number` 保留原附件及准备页定位。

多帧图片不支持非零旋转，包括动画 GIF、WebP。工具按图片实际帧数判断，返回 `status=failed` 和 `error_code=attachment_multiframe_rotation_unsupported`，表示“不支持多帧旋转”，不发送仅保留首帧的副本。该限制同样适用于 `--region`；默认 `--rotate 0` 保持现有读取行为，局部复读仍只支持单帧图片。

同时提供 `--region` 与 `--rotate` 时，先按原图像素坐标裁剪，再旋转裁剪副本。报告中的 `region`、`source_image_size` 和 `image_tiles[].box` 仍对应原图，覆盖范围仍为 `image_region`。超大图也先按原图分块，再旋转各分区；旋转不扩大读取范围，不改变原有 coverage 含义。

工具返回 `report_file`，报告保留逐页/分区回答和来源。使用 `--material-file` 生成的报告携带真实附件 ID，压缩包内页面归属于原压缩包，重复页面分别保留各附件归属。将整份原始报告与意见一起保存，SQLite 自动按附件 ID、定位和实际来源记录内容；无 `source` 的旧报告仍按 `deepseek` 处理，历史数据保持原样：

```bash
bash .codex/python.sh -m apps.oa.cli --root runtime submit --task-id "$OA_TASK_ID" \
  --opinion-file /absolute/path/opinion.txt \
  --recognition-file /absolute/path/recognition.json
```

定向读取同时生成 `recognition.json` 和 `observations.json`，返回 `observations_file`。观察保留真实附件 ID、原定位、实际来源、内容、区域/角度及报告路径和问题等来源信息，不同区域或角度可以区分。通过 `submit --observations-file /absolute/path/new-reread-directory/observations.json` 将复读结果作为补充保存，原始整份报告继续通过 `--recognition-file` 提供。直接原图观察由审核 Agent 实际查看后补充，工具不会生成这种观察。

上述提交入口默认只保存预览。只读识别工具不访问 OA、不提交意见，识别文字不是权威主数据，也不证明材料属于当前费用。

新材料的 `attachment_presence.status=confirmed_empty` 及 `evidence_refs` 记录官方附件来源已完整取得且无可下载附件。工具据此返回 `status=skipped`、`reason=official_no_attachments`，不调用模型；该结果仅结束附件识别，业务审核仍按[证据阅读规则](../../.agents/skills/oa-finance-preaudit/references/observation-contract.md#全页覆盖门禁)完整读取表单、明细及页面业务资料。空的资源或准备页列表本身不能证明无附件；采集/解析失败、声明附件未取得、准备不完整仍报告失败，历史材料沿用原有处理。

超大图按原像素分块识别，记录各分区坐标。原件不缩放覆盖，全部分区完成才表示该页识别完成；局部失败和视频未读取明确保留。高影响金额、主体、日期、里程等读数不稳定时定向复读，仍不能确定则在业务意见中说明缺口。

财务图片按[证据规则](../../.agents/skills/oa-finance-preaudit/references/observation-contract.md)读取，其他流程按当前匹配规则决定是否直接观察原图。压缩阅读副本见[图片压缩](image-compression.md)。
