# 图片压缩

```bash
python -m apps.oa_approval.image_compression \
  --file /absolute/path/source.jpg \
  --output-dir /absolute/path/new-reading-copy \
  --max-dimension 8192 --max-pixels 16000000 --max-bytes 8388608
```

工具保留原件，生成独立辅助阅读副本及处理报告。报告包含来源与副本指纹、尺寸、编码和缩放情况。目标参数限制文件大小，不保证小字、金额、日期或印章清晰；实际阅读不能确认时回原图或使用[DeepSeek 局部复读](attachment-recognition.md)。

支持格式与预算以入口 `--help` 为准。PNG 或透明图保留 PNG，其余静态图片可生成 JPEG。原图、PDF 原页、压缩副本和裁切区域分别保留来源，不把副本当成另一份 OA 附件。
