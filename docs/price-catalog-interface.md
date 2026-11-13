# PriceCatalog 接口

标准库实现，独立 SQLite 文件；新库没有任何种子价格。调用方负责授权、操作审计以及从已确认询价参数构造查询，模块不调用模型、不计算报价、不自动采用候选。

```python
catalog = PriceCatalog(path)
catalog.list_entries()                         # 所有条目版本，含草稿/已停用
catalog.get(entry_id, version)                  # 当前生命周期状态及完整版本内容
catalog.create(payload, request_id=event_id)   # 新条目 v1，状态 draft
catalog.new_version(entry_id, payload, request_id=event_id)  # 完整替代载荷，新 draft
catalog.publish(entry_id, version, request_id=event_id)
catalog.disable(entry_id, version, request_id=event_id)
catalog.match(context)
```

变更方法的 `request_id` 为可选关键字参数。API 将 `event_id` 从价格载荷剥离后传入，建议对每次变更均提供。同 ID 与相同规范化参数重放返回原始 receipt；不同动作或参数冲突。receipt 与价格状态在同一事务提交；重放原 publication receipt 不会使已停用条目重新发布。界面需要当前状态时重新 list/get。

## 价格载荷（平铺）

| 字段 | 约定 |
|---|---|
| `kind` | `material` / `operation` / `setup` |
| `name` | 非空显示名称 |
| `rate` | 非负十进制字符串；不接受 float、科学记数法、NaN 或负值 |
| `unit` | 材料仅 `CNY/kg`；工序 `CNY/piece` 或 `CNY/order`；准备费仅 `CNY/order` |
| `material_grade`、`material_form` | 必填；去首尾空白后精确匹配，不做牌号推断或通配 |
| `thickness_min`、`thickness_max` | 必填正十进制字符串，单位 mm，含上下边界且 min≤max |
| `product_family` | 工序、准备费必填；材料仅允许省略/null/空字符串 |
| `operation` | 工序必填，精确名称；其他类型仅允许省略/null/空字符串 |
| `qty_min`、`qty_max` | 可省略；规范化快照显式保存 1 / null，null 表示无上限；非空时正整数且 min≤max |
| `valid_from`、`valid_to` | 必填 `YYYY-MM-DD`，含首尾日期，from≤to |
| `currency`、`tax_included`、`freight_included` | 必须明确提供 `CNY`、false、false |
| `source` | 非空来源说明；不代表模块已验证来源真实性 |
| `simulated` | 必须显式提供布尔值，不以字段遗漏推断真实/模拟 |
| `inclusions` | 必填非空字符串数组，列明适用费用包含范围 |

不接受 `entry_id`、`version`、`status` 等由服务管理的属性作为价格载荷。create 自动生成条目 ID。所有内容一旦形成版本均不原地修改，编辑操作通过完整载荷创建新版本。

返回记录包含完整规范化载荷、`entry_id`、`version`、`status`、`created_at`、`published_at`、`disabled_at`、`snapshot_sha256`。哈希覆盖规范化价格内容、ID、版本与创建时间；生命周期状态/时间与不可变内容分开。报价选价时应复制完整候选及来源上下文，不只保存该哈希或 ID。

## 发布与停用

每个条目最多一个 published 版本。发布新版本会在同一事务中停用该条目此前的 published 版本，旧内容与哈希不变；新草稿不影响当前发布价。不能倒退发布旧草稿，也不能重新发布 disabled 版本。需要恢复旧价格时以其内容建立新版本。

发布是立即替代，不是预约调价：若新版本尚未到生效日期，匹配不会偷偷回退到旧版。停用最新版后同样不会复活历史价格。不同条目存在适用条件重叠时保留歧义，由人选择，不自动按最便宜或最新条目决定。

## 匹配

`context` 接受 `material_grade`、`material_form`、`thickness`（正十进制字符串，mm）、`quantity`（正整数）、`product_family`、`process_route`（无重复的名称数组）。可提供 `as_of` 日期用于复算；省略时按北京时间当天。其他查询属性不被静默忽略，返回 unsupported。

材料匹配要求牌号、形态、厚度、数量完整；工序和准备费还要求产品族，工序另要求工序集合。缺少某类别需要的条件时不搜索宽泛替代。所有数值区间使用 Decimal/整数比较，不经 SQLite 浮点转换。

返回 `{as_of, results:[{kind, operation?, status, candidates, missing, requires_confirmation:true}]}`：

- `unique`：一条当前有效的完整 published 快照，仍需人工确认。
- `ambiguous`：多条当前有效快照，不自动选第一条。
- `missing_conditions`：列出尚缺条件，候选为空。
- `not_found`：没有当前适用价；只有未来生效价格时也使用该状态。
- `expired`：适用条件相符的发布价已经过期，候选为空。
- `unsupported`：字段类型、数值、日期、重复工序或额外维度不受支持，候选为空。

draft、disabled 和过期价格不作为可选择候选返回。匹配仅过滤价格条件，不能验证材料工程替代、实际工艺或包含项语义。现有计算器对按单工序费与正准备费的冲突校验仍需保留。

## 工作台采用边界

查询候选不会改写询价费用。界面明确选择候选并带入表单后，仍须提交技术信息；业务 API 同时收到实际费用值与 `{entry_id, version}`，复查当前条件、版本和金额/单位一致性，再把完整候选写入原始表单来源快照。材料匹配条件齐全时可先采用材料价，工序未确定不应阻断这种部分补录，也不代表整单已可报价。

已采用价的数量、材料、厚度等条件或有效发布版本发生变化时，新报价需重新匹配或由操作者明确核对相应费用。仅修改其他费用不能解除尚未核对的旧价格引用；部分采用新价格也须保留其他未提交费用的引用。已生成报价继续读取自己的记录及定价快照，价目表变化不修改旧报价。

错误为 `PriceCatalogError(ValueError)`，带 `code`（例如 `invalid_argument`、`not_found`、`conflict`、`integrity_error`、`unsupported_schema`）。SQLite 文件应单独备份；在线备份使用 SQLite backup 能力或先停止写入，不能只复制仍在写入的 WAL 主文件。
