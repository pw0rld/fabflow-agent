# FabFlow Agent

AI 辅助的 CAD 拆件、工程参数提取、BOM 与制造报价工作台。

当前代码来自已运行的钣金工程原型，作为通用化重构的起点。部分模块仍保留早期演示名称、路由和费率；工厂价格、设备能力、工艺规则与模板的统一配置化是后续工作。

## Agent 运行层选型

2026-11-14 确定采用 **OpenAI Agents SDK（Python，包名 `openai-agents`）**，由 SDK 承担模型与函数工具的执行循环。FabFlow 保留 CAD 工具、来源校验、后台任务、订单权限、预算控制和不可变报价事务。模型供应商通过配置接入，具体图像、工具与协议兼容性须在实际接口上验证。

本分支将图纸读取、订单资料归类、工程提参、CAD 分组建议和报价修改意图接入进程内的 SDK 执行层。读图 Agent 只有受附件清单约束的 `read/read_image`；报价修改意图不提供工具。候选来源校验、人工核对与确定性计价继续由业务代码负责。

后台会话保存在工作目录，保留 `start/inspect/stop` 契约、真实工具进度和完整 JSON 结果；响应不明或进程重启后不会自动重发已提交请求。旧 DSH 传输须显式选择，SDK 故障不会自动切换并再次调用模型。图片分区服务保留固定的两阶段 Responses 工作流，与 Agent 共用显式模型配置。

此分支不自动修改生产部署。SDK 协议与业务回归在隔离服务器验证；离线模拟接口测试不代表真实图纸识别精度或第三方中转兼容性。官方依据见 [Agents SDK 快速开始](https://developers.openai.com/api/docs/guides/agents/quickstart)、[运行与状态](https://developers.openai.com/api/docs/guides/agents/running-agents)。配置和限制见 [SDK 运行层](docs/agents-runtime.md)。

## 工作流程

```text
STEP / 图片 / PDF
        │
        ├─ STEP：原生 CAD 读取 → 装配 / 实体拆分 → 几何量测
        └─ 图片 / PDF：预览与分图 → AI 提参 → 来源校验
                                      │
                         零件清单 / 工艺候选 / 待确认项
                                      │
                         工程方案与材料、采购条件确认
                                      │
                         确定性成本计算 → BOM / 报价草稿
                                      │
                                  人工审核
```

## 已有模块

- STEP 装配与实体读取、三维总装和单件预览、经校验的单件 STEP 导出。
- 原生体积、面积、局部板厚、平板轮廓与孔槽、折弯候选。
- 图片自动分区、原像素裁图、带来源的参数提取与人工校正。
- BOM 汇总、净重估算、工艺工程量估算、Decimal 计价和报价打印。
- 客户订单归属、内部成本隔离、上传限额、不可变报价版本与中断恢复。
- OpenAI Agents SDK 模型接入、只读附件工具和报价修改助手；保留显式 DSH 兼容选项。

候选参数和模拟报价需要审核。折弯件的报价代理量不代表生产展开或机床刀路；DWG/DXF 的显示能力也不代表已完成对应制造数据提取。演示费率不是工厂实际采购价。

## 代码位置

| 目录 / 文件 | 职责 |
| --- | --- |
| `src/agent_bridge.py` | 选择 SDK / DSH，衔接读图任务契约 |
| `src/agent_runtime.py` | 持久会话、后台执行、停止与重启恢复 |
| `src/agents_executor.py` | SDK 模型循环、用量与调用预算 |
| `src/agent_tools.py` | 附件清单授权的文字与图片读取 |
| `src/dsh_bridge.py` | 共用读图提示与输出校验、旧 DSH 传输 |
| `src/cad_geometry.py` | 原生 STEP 解析与几何输出 |
| `src/cad_quote_quantities.py` | 带来源的报价工程量 |
| `src/batch_quote_service.py` | 订单、处理任务与报价版本 |
| `src/batch_process_estimate*.py` | 工艺估算、方案采用和报价事务 |
| `src/batch_cost_sheet*.py` | 内部核价结构与费用采用 |
| `src/workbench_server.py` | HTTP 服务入口 |
| `drawing_split_service/` | 图片分图、提参与校正服务 |
| `frontend/` | React / Next.js 工作台源码 |
| `web/` | 手写页面、浏览器 CAD 查看器及其独立依赖 |
| `skills/` | 询价整理与报价业务规则 |
| `tests/` | 合成数据与公开 CAD 样本的测试源码 |
| `benchmark/` | 评估引擎与数据契约，不包含私有评测集 |

## 开发环境

- Python 后端；原生 CAD 依赖见 `requirements-cad.txt`，订单预处理依赖见 `requirements-orders.txt`。
- 前端使用 Node.js、pnpm、React 和 Next.js；版本以 `frontend/package.json` 与锁文件为准。
- SDK 默认在后端进程内运行，无需单独部署 DSH；模型和凭据由服务端环境显式提供，仓库不包含 API key 或代理节点配置。
- 本项目当前维护约定：测试、原生计算和前端编译在配置好的远程构建服务器执行，本机只编辑与同步。

在远程开发目录安装依赖并启动后端：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-cad.txt -r requirements-orders.txt

# 在服务器环境配置 OPENAI_API_KEY；不要写入命令记录或前端配置。
export FABFLOW_MODEL=your-vision-capable-model
export FABFLOW_MODEL_API=responses

.venv/bin/python src/workbench_server.py \
  --root /var/lib/fabflow/tasks \
  --price-db /var/lib/fabflow/prices.sqlite \
  --reader-root /var/lib/fabflow/reader \
  --agent-backend openai-agents \
  --port 3092
```

后端默认仅监听 loopback。对外提供服务时，需配置反向代理、内部管理接口认证及独立的数据目录，参见 [部署与数据边界](docs/development.md)。

在同一远程构建环境生成工作台静态页面：

```sh
cd frontend
pnpm install --frozen-lockfile
HYL_DEMO_BASE_PATH=/workbench pnpm build
```

将 `frontend/out/` 发布到实例的 `web/demo/`。数据库、上传资料及订单结果须保存在静态目录之外。

## 公开源码范围

仓库不包含真实客户图纸、订单、报价记录、Excel 成本表、历史截图或生产配置。`src/customer_costing_reference.py` 为未配置的空参考适配器；不会向订单注入客户样例价格。需要工厂参考值时，应另行配置并保留来源与采用记录。

`samples/` 只包含带来源及许可说明的公开 CAD。第三方查看器的许可证和来源说明随依赖保留，见 [第三方说明](THIRD_PARTY_NOTICES.md)。本仓库没有为自有代码指定开源许可证；公开可见不代表已授予额外使用许可。
