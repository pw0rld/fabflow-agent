# OpenAI Agents SDK 运行层

后端默认使用 Python `openai-agents==0.24.0`。SDK 负责模型与函数工具循环，FabFlow 负责订单权限、来源和候选校验、持久任务、资源限制及确定性核价。原生 CAD、重量、BOM 与金额计算不会交给模型自由计算。

## 配置与启动

在远程开发环境安装 `requirements.txt`，原生 CAD 与订单依赖分别见 `requirements-cad.txt`、`requirements-orders.txt`。`.env.example` 仅列出配置项，应用不会自动读取 `.env`；用部署环境或权限受限的服务环境文件注入，禁止提交真实凭据。

| 配置 | 含义 |
| --- | --- |
| `FABFLOW_AGENT_BACKEND` | `openai_agents`（默认）或显式 `dsh` |
| `FABFLOW_MODEL` | 必须显式指定实际接口提供的模型，无默认模型回退 |
| `OPENAI_API_KEY` | 服务端凭据，亦支持 `FABFLOW_OPENAI_API_KEY` |
| `OPENAI_BASE_URL` | 可选 API 地址，省略时使用 OpenAI 官方地址；亦支持 `FABFLOW_OPENAI_BASE_URL` |
| `FABFLOW_MODEL_API` | `responses`（默认）或 `chat_completions` |
| `FABFLOW_MODEL_STORE` | 默认 `false`；不代表第三方模型服务没有自己的数据保留政策 |

标准 `OPENAI_*` 变量优先于同义的 `FABFLOW_OPENAI_*` 变量。缺少模型或凭据不会阻止纯 CAD 服务启动，启动 AI 任务时才拒绝未配置的请求。配置错误须修正后显式重试。

```sh
.venv/bin/python src/workbench_server.py \
  --root /var/lib/fabflow/tasks \
  --price-db /var/lib/fabflow/prices.sqlite \
  --reader-root /var/lib/fabflow/reader \
  --agent-backend openai-agents --port 3092
```

第三方接口需要支持选择的 API、流式响应、函数调用、图片输入与用量返回。图片分区服务仍是固定两阶段调用，目前仅支持 Responses；它共享上述模型配置，需要代理时另设 `DRAWING_SPLIT_PROXY`。Agent 的 OpenAI 客户端遵循其 HTTP 环境代理配置。不得把 API key 放进前端、上传附件或模型提示词。

## 已衔接的入口

| 业务入口 | 执行路径 |
| --- | --- |
| 图片 / PDF 读图 | `AgentBridge` → SDK → 带来源的候选参数校验 |
| 订单资料归类 | `DiscoveryBridge` → SDK → 归类结构校验 |
| 批量工程提参 | `batch_engineering_ai` → SDK → 工程证据校验 |
| CAD 分组建议 | `cad_manufacturing_ai` → SDK → 原生实体引用校验 |
| 报价修改意图 | `batch_quote_assistant` → SDK，工具清单为空 → 确定性修改预览 |

共用提示与校验暂保留在 `dsh_bridge.py`。内部 `session.*` 方法用于兼容现有调用，默认没有 DSH HTTP 请求。历史 `.dsh-*` 清单保留以维持资料身份，文件名不决定运行后端。

## 附件与结果边界

模型只能用 `read`、`read_image` 读取服务端登记的附件 ID 或精确路径。工具验证来源哈希、文件类型、目录和大小；不会扫描工作目录来扩大权限。图片工具返回经过校验的原始图片数据。PDF 需先由现有读取预处理生成登记页图，不把二进制 PDF 当作图片。

已生成的 PDF 页图和局部图需要绑定原件。凭据、配置、任务状态、AGENTS 文件及报价结果不能通过附件工具读取。上传内容仅作为业务资料；不增加工具权限。工程候选仍保持 `pending_review`，未知数值不填零。

最终输出必须是一个完整、有限大小的 JSON 对象，再通过各业务模块校验。语法有效不代表工程判断准确。进度只反映任务和工具事件，不显示推理过程、工具正文或虚构百分比。

## 调用、用量与恢复

| 限制 | 默认值 / 环境变量 |
| --- | --- |
| 每次 Agent 执行时限 | 600 秒，`FABFLOW_MAX_RUNTIME_SECONDS` |
| 模型请求数 | 12，`FABFLOW_MAX_REQUESTS` |
| 已观察 token 用量 | 100000，`FABFLOW_MAX_TOTAL_TOKENS` |
| 单次输出 token 上限 | 12000，`FABFLOW_MAX_OUTPUT_TOKENS` |
| Agent 轮次 / 工具调用 | 12 / 64，`FABFLOW_MAX_TURNS` / `FABFLOW_MAX_TOOL_CALLS` |
| 提示 / 最终输出字节 | 2 MiB / 1 MiB，`FABFLOW_MAX_INPUT_BYTES` / `FABFLOW_MAX_OUTPUT_BYTES` |
| 单工具 / 累计工具输出字节 | 32 MiB / 64 MiB，`FABFLOW_MAX_TOOL_OUTPUT_BYTES` / `FABFLOW_MAX_TOTAL_TOOL_BYTES` |
| 同进程并行执行 | 最多 2 个，超出时在提交模型请求前拒绝 |

总 token 限制按模型返回的实际用量检查，缺失用量则不能报告为零或成功。它可以阻止后续请求，无法给已经发送的输入提供精确的费用硬上限。业务的补读可能创建另一个受限 Agent 执行；这不是跨整张订单的总费用封顶。进程并发限制也不是跨多实例的全局队列。部署仍须限制工作进程数量和资源。

客户端自动重试关闭，SDK Runner 不配置额外重试。每次执行禁用 SDK tracing，不改动进程全局 tracing 设置。会话、来源快照、完整输出和用量保存在私有工作目录的 `.fabflow-agent-runtime/`；配置快照不保存凭据或接口地址。

请求在发给模型前持久登记。相同会话与请求重放只读取已有状态；进程重启发现未完成执行时标记中断，不自动重发。停止和超时结束本地等待，不能保证模型供应商立即停止计费。显式重试可以产生新的付费请求。

旧 DSH 兼容模式须使用 `--agent-backend dsh --dsh-url http://127.0.0.1:3080`。切换后不自动恢复另一后端的在途任务，应先停止接收新任务并确认旧任务状态。已有不确定结果不会自动派发到新后端。

## 服务器验证

只在独立远程目录执行，并对测试进程设置 CPU、内存和时限。以下测试使用合成附件、假执行器或离线模拟接口，不调用真实模型：

```sh
PYTHONPATH=src:tests:. .venv/bin/python -m unittest \
  test_agents_executor test_agent_tools test_agent_runtime \
  test_agent_bridge test_agents_sdk_transport -v
```

实际 SDK 的流式协议、图片工具循环与失败行为须与纯单元测试分开检查。上线前还应对实际接口执行另行授权的公开样例测试；离线协议通过不能证明模型看懂了图纸。本分支不修改现有生产服务。

2026-11-14 在 ZeroClave 的独立构建目录完成 **344 项相关回归，全部通过、无跳过**，涵盖上述 SDK 测试，以及原有候选校验、PDF 页图与局部图、订单恢复、工程提参和核价隔离。真实 SDK 的 Responses / Chat Completions 各验证了两轮图片工具调用及 503 不自动重试；HTTP 使用离线模拟传输，没有调用外部模型。另用 `tools/smoke_agent_server.py` 验证无模型凭据时实际 CLI 仍可启动并通过健康检查，使用临时订单目录与随机本地端口。测试 CPU 限制 70%，内存上限 512 MiB。该结果不是全仓库 CI 或图纸识别精度 benchmark。
