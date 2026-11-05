# 关户只读数据入口

按本次实际使用的数据源读取对应段落。凭据走已有私有配置，配置或接口不可用时记录实际错误及影响，不用旧样本填当前结果。真实数据、认证和完整账号不进入Skill或公开提交。

## OA

Python入口、环境和参数以项目README及`--help`为准。复用`apps.oa.approvals.sources.oa.build_api`及`load_source_policy()`：`pending_rows()`读取当前身份待办，`request_context(request_id)`取得表单和资源。实际节点通过现有官方SQL读取能力按申请ID核对；转发节点、`canEdit`和待办命中不能证明实际流转位置。

已核字段：`lcbh`流程编号、`gsdm`公司、`jxsdm`客户编码、`jxsmc`客户名称、`je`金额、`yetk`余额分支、`sftk`是否退款。当前选项为`yetk=0`有余额、`yetk=1`无余额，`sftk=0`是、`sftk=1`否；使用前核当前表单定义。当前余额0不自动将历史有余额退款单改成无余额单。

公开流程键为`dealer_closure`。collect将本次来源及提交上下文冻结到`dealer_closure_evidence`，审核Agent以此为准。正常审批与未阅转发批注分开：转发需唯一投递ID、原转发日志、批注按钮及实际节点；已阅转发不重复处理。写入前重新核对上下文，批注反馈回查原节点保持不变。

连接设置位于私有OA认证文件同目录的`dealer-closure.json`，要求普通文件、权限600。包含Doris解释器/连接模块路径，SAP解释器/SDK路径、正式环境标识和钥匙串服务，DMS正式基址。口令通过已有钥匙串取出，仅在子进程标准输入中传递，不保存到材料或命令行。配置缺失或读取失败不形成已完成意见。

私有配置中的冰柜确认须绑定申请、唯一投递ID、公司、客户、确认日期及依据；仅匹配本次投递时进入冻结事实，不以当前批次历史确认泛化新单空值。

## BI

复用已配置Doris只读连接；本机已有模块时可用`~/.config/doris/connect.py`的`connect()`。使用后关闭连接，保留全部科目行及批次。

```sql
SELECT month_id,company_code,cust_code,cust_name,
       cust_balance,cust_balance_display,remain_balance,
       ledger_account_control,ledger_account_name,pre_end_balance,
       gl_special_code,gl_special_balance,cust_status_credit,
       freezers_qty,three_financing_amt,no_subsidy_amt,create_date
FROM hive.dw.dwd_fin_cust_balance
WHERE company_code=%s AND cust_code=%s;
```

无余额单也保留原记录，不加`cust_balance<>0`。`create_date`是表批次，不是SAP业务截止时点的独立证明。

| 字段 | 用途 |
| --- | --- |
| `cust_balance` | 客户余额，多科目行可重复，不直接逐行相加。 |
| `cust_balance_display` | 余额显示，结合来源明细和聚合口径核对。 |
| `pre_end_balance` | 对应独立预提科目的余额。 |
| `gl_special_balance`、`gl_special_code` | 特别总账金额及标识，成对核对。 |
| `cust_status_credit` | 报表权限状态，必要时核实际冻结字段。 |
| `freezers_qty` | 冰柜数量；NULL不证明0台，另存业务确认客户范围与日期。 |
| `three_financing_amt`、`no_subsidy_amt` | 约定的融资和发货后补贴结果，已知历史覆盖问题另列。 |
| `remain_balance` | BI独立字段；映射未确认时不代替SAP下单剩余，不用于认定同客户账面余额错误。 |

不明粒度、期间和血缘写技术核对。查血缘定向读取实际生成任务及目标源记录；权限拒绝记所缺定义，不将候选SAP快照认作既定上游。

## 正式SAP

已核基准为`100.100.1.35 / P01 / sysnr01 / client600`，客户端为Production Client。连接后用`RFC_SYSTEM_INFO`、`T000`核环境；配置变化先取得正式依据，测试结果单独保存。

复用私有连接及RFC运行环境。只读接口包括`RFC_SYSTEM_INFO`、`RFC_READ_TABLE`、`RPY_PROGRAM_READ`。`ZSD_CRM_CREDITLIMIT_EXPORT`相关实现会写查询日志，`ZFI029S`保存程序会写表，其执行不属于只读路径。源码读取与报表执行分别记录。

### 下单剩余和占单

当前`ZSD025`对应`ZSSR16`：

```text
KALLK = KLIMK - SKFOR - SSOBL
VKUKW = VKOKW + VAOKW + VLOKW + VFOKW
VKRKW = KALLK - VKUKW
```

依次为总额度=信贷限额-应收总额-特别债务，占用=合同+订单+交货+开票，下单剩余=总额度-占用。尾负号`12500.00-`表示负数，用十进制计算。

核`KNKK`信用范围及参考账户、`KNVP`付款方、当前合并组，再按活动源码读`S066`、`S067`、`VAKPA`及合同状态。三个KNKK金额不能替代占用核对，BI占单0也不证明SAP组件全0。归属、作用域和读取完整后输出复算，保存组件及原值。

未清合同按付款方及销售组织关联，核`VBAK-VBTYP=G`、`VBUK-RFGSK=A`、`VBAP-ABGRU`为空及`VBUP-RFSTA=A`。合同占用取有效行`KZWI2`，减去`VBFA-VBTYP_N=C`对应后续订单行的`KZWI2`，保留币种、行号及关联原值；只有合同号和状态不足以复算金额。

客户账差异按公司/客户读完整`BSID`及适用期间`BSAD`，按`SHKZG`核借贷。特别总账核`UMSKZ`实际行项目，不能只用`SSOBL=0`替代。

### 预提及处理凭证

当前`ZFI029`对应`ZFRR04`。解释差异时读活动源码：已有实现从`BSIS`取总账，再以凭证、年度、行号读`BSEG-XREF1`，去空格、按客户补零关联。仅按客户往来`KUNNR`找不到该科目不证明预提0。

按原公司、科目及客户归属核完整余额，凭证回查使用精确主键，保留`BKPF-STBLG`冲销关系。跨科目同额或已冲销记录不抵消原余额。按活动源码解释`SHKZG`和`XNEGP`，避免重复反号；非未清项管理科目不以`AUGBL`空白单独断言未处理。

保留`RFC_READ_TABLE`请求、条数和完整性。到上限不视为完整；分页失败保留失败，再用可证明完整的单次索引或精确凭证批次。索引全部归属后才作全量余额结论，避免对聚簇明细无主键大范围扫描。

### 融资、实际发货和冻结

当前`ZFI060`对应`ZFRR08`，已有实现按回款类型、过账日、抬头和客户归属读取并排除冲销。复查以当前源码为准；期初导入汇总不证明旧明细已覆盖。

实际发货用`LIKP-WADAT_IST`等实际出库记录，不用计划日`LFDAT`替代。空结果只说明所查范围未取得，继续核售达方/付款方及历史范围。

`KNKK-CRBLB`信用冻结、`KNVV-AUFSD`订单冻结、`LIFSD`交货冻结、`KNB1-SPERR`过账冻结含义不同，分别核对，不用任一字段证明全部权限已锁。

## DMS

首次使用或接口变化时读[正式接口文档](https://klvcodtump.feishu.cn/docx/Jbl9dGZdQoJNV7xjJZXckVejnPg)，按其正式地址调用：

```text
POST {正式DMS基址}/api/v1/json/shareApi
Content-Type: application/json
{
  "interfaceTag": "queryLatestSignedBillByCustomerCode",
  "reqJson": "{\"customerCode\":\"目标客户编码\"}",
  "reqTime": 毫秒时间戳
}
```

| 需求 | 查询标签 |
| --- | --- |
| 最新已签对账单 | `queryLatestSignedBillByCustomerCode` |
| 退款申请书 | `queryLatestRefundApplyByCustomerCode` |
| 账户报备 | `queryPaymentAccountReportByCustomerCode` |
| 适用1+N合同 | `querySignedOneAddNContractByCustomerCode` |

核HTTP、业务成功标志、返回客户编码及文档状态枚举，保留最新月份和合同编号。对账单当前`contractStatus=4`为签署完成，其他合同按其实际状态定义，不能凭文件存在判签署。

使用正式外部查询，不要求登录DMS门户。认可状态后，不因普通预览件无章要求补签。退款内容等确需文件时，优先用返回`fileUrl`或OA有效原件；前端预览路由不是唯一下载协议，某次空正文不证明整套签署失败。

`result=null`、状态缺失、账户未返回与技术失败分别记录。有有效OA原件或已核报备时保留实际来源，不由一个接口为空推导材料必缺。完整账号、身份证、电话及认证信息仅存私有证据。
