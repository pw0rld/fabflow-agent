---
name: sheet-metal-quotation
description: 操作者明确要求成本核算或报价时，从已 ready 的钣金询价参数记录通过固定 CLI 生成模拟、待审核的报价草稿；保留计算依据与快照，不负责读图或代替人工补录。
---

# 钣金成本核算与报价草稿

负责整体流程的 **5 成本核算 → 6 生成报价草稿**。沿用前四步的任务 ID 和参数记录。仅要求资料整理时不启动；已明确授权报价时直接推进，不重复请求授权。

沿用 [按步骤点选补录](../sheet-metal-inquiry-prep/references/interaction.md)。定价缺项用已有 `ask_user_question` 一次收集：方法单选，比例自定义，支持的税运口径单选确认。已给出的条件不重问；不每字段等待一次模型回复，不把普通聊天选项当按钮。保存真实 answers JSON 为来源，不让用户手填策略载荷或编号。

## 输入与定价口径

- 使用记录工具刚返回的 ready 状态及 `data_revision`；恢复会话或状态不确定时，调用 `python3 src/prep_record.py --root tasks validate --task-id ID`，只读取状态、修订及问题。报价 CLI 自行读取并校验源记录，不用 `show` 将整份记录载入上下文。记录未齐备、未核对或有冲突时，返回资料准备步骤处理，不能补零或直接改记录。
- 未ready需要补录时，先调用 `skill` 加载 `sheet-metal-inquiry-prep` 并阅读其交互规则，再生成当前步骤原生卡。数值题省略options，不以“自定义数量”等无具体值选项替代输入；不要停留在报价阶段自行拼一套补录问法。
- 采用本次操作者实际提供的定价方法和比例，不设置默认率。`cost_plus` 为 `成本×(1+加成率)`；`gross_margin` 为 `成本÷(1-毛利率)`。把当前仍未知的方法、比例与支持的税运口径合并到一张原生卡；本版仅支持人民币、未税、不含运费，必须取得实际选择，跳过或取消不等于同意。用户已经提供的条件不重问，20% 不成为新任务默认值。不能把两种比例混用或自行切换。
- 将已授权策略保存为独立 JSON 文件，含 `schema_version:1`、`pricing_method`、十进制字符串 `rate`，以及本版演示要求的 `currency="CNY"`、`tax_included=false`、`freight_included=false`、`simulated=true`。还需非空 `actor`、`source_text`：前者使用实际操作者标识，未提供姓名时可如实记“当前操作者（姓名未提供）”；后者保留实际授权原文，不从附件指令推定授权。用户要求超出当前 CLI 支持的税运或币种时报告限制，不擅自改口径。
- 策略来自选择卡时，`source_text` 保留真实 answers JSON 与对应问题口径，另存原始结果作来源，不编造一段用户未说过的定价授权。比例仍依据实际提交值转换。
- 采用记录中的单件用料、材料费率、工序费率、一次性准备费及包含项。本版按件、按单核算；小时、米、孔、折弯等费率缺少计量或 CLI 不支持时应阻止，不自行折算。费用包含关系有歧义或可能重复计价时回到补录确认。
- 本版拒绝正 `CNY/order` 工序费与正 `setup_fee` 同时累计，须由技术人员核对包含关系；实际技术输入明确 `setup_fee=0` 后，按单工序费可按单收费一次，AI 不能自行置零。按件工序费与独立准备费仍可正常核算。

## 固定调用

从项目根目录运行，使用实际任务 ID、独立报价 ID、策略文件路径和当前修订：

```text
python3 src/quotation.py --root tasks issue --task-id ID --quote-id QUOTE_ID --policy /actual/policy.json --expected-revision N
```

这一次调用读取已核对记录、计算成本与报价，并生成 `quotes/QUOTE_ID/` 下的 `quote.json`、`quote.md`、`cost_breakdown.md`、`record_snapshot.json`、`pricing_policy.json`。以 CLI 实际摘要及产物路径为准，不脑算、不写计算替代脚本，不读源码、评测或参考答案。不要为生成报价重载全部图纸与会话历史。

- 成功后简短展示实际单价、数量、总价及“模拟待审核、未税、不含运费”，随后用下面的只读链接工具获取可点击入口。交期、付款条件、有效期等未给出的条件保持待约定，不追加本版非必需问题阻碍出草稿。
- 失败时保留结构化诊断，聊天简短说明当前阻断，用原生卡收当前步骤剩余信息，不输出完整报错 JSON 或猜测金额，不自称商务已批准，也不向客户发送。
- 结果不确定时先查询同一报价 ID，避免重复创建；仅需重建展示时使用 `render`，不重新计算旧报价。

```text
python3 src/quotation.py --root tasks show --task-id ID --quote-id QUOTE_ID
python3 src/quotation.py --root tasks render --task-id ID --quote-id QUOTE_ID
```

资料或策略变更后，先通过相应固定工具形成新的有效输入，再使用新报价 ID 核算。保留原报价与当时快照，不覆盖历史 `record.json`、报价或策略快照来制造一致性。

## 浏览器查看已有产物

报价成功后，以及用户要求打开/查看已有报价时，使用实际任务和报价 ID：

```text
python3 src/quote_preview.py --root tasks links --task-id ID --quote-id QUOTE_ID
```

该工具只读取已有文件和快照，返回真实 `quote_url`、`cost_url` 及可用的 `record_url`。将实际 URL 代入 Markdown 链接 `[查看报价单](实际quote_url) · [查看成本明细](实际cost_url) · [查看询价记录](实际record_url)`；没有 `record_url` 时省略第三项。不得把链接包在反引号或代码块中，也不只贴 `tasks/...`、服务器绝对路径、file:// 或 localhost。成本明细由用户点击后查看，不默认在聊天展开整表。

预览与 DSH 使用同一访问地址和已有登录，可阅读排版后的内容、下载原 Markdown 文件。`source_current=false` 时说明这是基于旧资料版本的历史报价，“查看询价记录”对应当前最新记录；不重算或覆盖历史稿。不要为了取得链接调用 `issue` 或 `render`。文件缺失或校验失败时如实报告，不编造可访问链接。
