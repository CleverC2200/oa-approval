# 最小审批执行接口

当前执行由 `apps.oa.cli` 和 SQLite 完成。业务判断由审核 Agent 按对应流程规则形成。

## 采集与提交

`collect` 完整采集并冻结当前单据材料及适用规则，审核 Agent 读取本次材料和规则，交回最终意见文本。`submit` 保存意见，当前授权明确时提交并回查。采集失败、材料全空或关键原件不可读取时保留失败，不生成貌似完成的意见；正常材料中的业务缺项按原规则表达。

## 附件观察

财务和包装入口使用最终意见文本及可选附件观察、识别报告。固定审核项、顺序、状态、证据含义和原值计算要求由 Agent 完整执行，公开意见格式见[共享结果接口](result-contract.md)。代码不替 Agent 形成业务判断，也不自动把过长意见改写成结论。

附件观察通过 `submit --observations-file /absolute/path/observations.json` 交回。文件顶层是 JSON 数组，每项绑定当前任务已有的附件资源，例如：

```json
[
  {
    "resource_id": "本单材料中的真实附件ID",
    "locator": "第1页／金额区域",
    "source": "agent",
    "content": "本页已读取的原值、单位、对应明细及其核对依据；未确认的读数或配对关系明确说明。",
    "status": "read"
  }
]
```

- `resource_id`、`locator`、`content` 必须提供。资源 ID 使用本单 `resources` 或 `prepared` 中已绑定的真实值；未绑定资源会拒绝保存。压缩包子页、重复页仍对应各自真实归属。
- `locator` 写明页码、区域、单元格或可定位的文本位置；涉及多张照片、复读或多组计算时，在相关附件的观察内容中保留各来源定位与对应关系，不把不同次观察混成一个无来源结论。
- `source` 区分 `parser` 程序解析、`deepseek` API 识别和 `agent` 的观察或综合核对。Agent 引用模型回答时保留原报告来源，不声称自己直接看到了原图。`status` 如实填写读取状态，`read` 只表示该内容已经读取，不代表业务审核通过；已读取内容中的未知、冲突写明具体事实，尚未读取或读取失败不得记作 `read`。省略这两个字段时，当前存储默认分别使用 `agent`、`read`，不能用默认值冒充已读。
- 表单和商旅权威事实使用完整 `declared_fields.path`、`authority_facts` 中的来源路径及本次工作上下文定位。没有真实附件归属的字段事实不编造附件 ID，也不强行塞入附件观察。
- DeepSeek 工具生成的整份原始报告可通过 `--recognition-file` 一起保存；其原始回答和来源保持原样，Agent 的补充判断另放附件观察。已有工具生成的观察数组也可直接传入。

默认预览的保存示例，`OA_TASK_ID` 使用本单实际任务 ID：

```bash
bash .codex/python.sh -m apps.oa.cli --root runtime submit \
  --task-id "$OA_TASK_ID" --opinion-file /absolute/path/opinion.txt \
  --observations-file /absolute/path/observations.json
```

## 审核未完成的人工交接

技术失败或超时不能自动转成行政意见提交。未形成业务意见时报告当前单据未完成；原固定业务要求不因此省略。

## 提交与回查

默认预览。实际提交要求当前明确授权，提交前确认当前单据与冻结材料未变。先保存提交开始记录，再调用 OA，结果不明保持 unknown，仅只读回查，不自动重发。旧材料和已提交意见用于回查，不改写或按新规则自动重审。
