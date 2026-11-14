# 客户试用入口

独立客户页面由 `frontend/app/experience/page.tsx` 与 `customer-trial` 组件提供，规范静态路径为 `/workbench/experience/`。部署时使用 `deploy/trial_access_config.py` 生成服务器级 Nginx 片段，将 `/try/` 精确映射到该页面；`/try` 重定向补斜线，其他 `/try/` 后缀返回 404。

页面只调用已有 `/workbench/customer/api`，不会加载操作员入口或内部成本接口。会话 Cookie 的 Path 仍是 `/workbench/customer/`，浏览器请求该 API 时会正常携带；不需要扩大 Cookie 范围，也不接受浏览器提供模型 API key。模型配置沿用服务端 Agents SDK 环境。

## 客户流程

1. 选择一个非空 STEP / STP 文件，最大 20 MB，填写根装配套数。
2. 上传与开始处理采用不同的事件 ID。成功提交后后台继续，页面仅 GET 轮询状态；结果不明确时显式恢复同一请求。
3. 查看原始装配、零件、数量、材料、板厚及预估重量。显示真实结果，缺项不补造数值。
4. 查看客户报价草稿及打印页；工艺范围未完整时显示待核价或已知小计。沿用既有客户工程方案确认流程，形成新报价版本。

只有数量是上传时的可编辑条件；没有提供尚未接通核价的材料或表面处理下拉框。历史询价依赖原有浏览器客户会话，不是跨设备账号登录。

上传限制沿用全局合计 10 次、每次一个 STEP 的现有账本，不能通过新页面获得额外额度。既有订单的查看、GET 轮询和模型预览不重新消耗上传额度。该限制不是模型费用的精确硬上限，模型执行预算另由 Agent 配置负责。

## 构建与验收

只在隔离的远程构建目录执行类型检查、Next 静态导出和 Playwright。不要在开发者电脑运行这些命令。

```sh
PYTHONPATH=src:tests:. python -m unittest test_customer_trial_access -v
cd frontend
pnpm exec tsc --noEmit
HYL_DEMO_BASE_PATH=/workbench NEXT_TELEMETRY_DISABLED=1 pnpm exec next build --webpack
```

静态导出后，后端启动时登记新的 `web/demo/experience/index.html` 与生成的静态资源。部署可只合并新页面与所需 `_next` 文件，保留既有页面、客户成本适配器、模型环境及订单数据。

```sh
TRIAL_ORIGIN=http://127.0.0.1:3393 TRIAL_PATH=/try/ \
TRIAL_BROWSER_OUT=/absolute/new/receipt-directory \
node tools/browser_customer_trial.cjs
```

浏览器脚本拦截全部客户 API，使用合成数据检查桌面、手机、文件校验、后台轮询、额度耗尽及同事件恢复，不向生产上传或调用模型。上线时还需验证真实 Nginx 的 `/try/`、静态资源、客户会话和内部管理接口认证；不要把仅有页面源码当成已部署或验收通过。

`tools/browser_customer_trial_live.cjs` 另做真实匿名会话检查：仅允许 GET/HEAD 及创建会话的 POST，阻止文件上传与其他 POST。它验证真实页面载入、会话 Cookie、订单和额度接口，不消耗上传次数或模型调用。

2026-11-14 在 ZeroClave 完成 6 项后端测试、TypeScript 检查、Next 静态编译和 6 个隔离浏览器场景（10 项流程检查）。实际 WebGL 渲染了绑定来源的两实体合成模型；手机布局、参数、完整报价与部分小计、同事件恢复均通过。用户授权后独立 `/try/` 入口已发布，真实匿名浏览器的会话、询价和额度接口均返回 200；内部接口仍要求认证。原有页面、工厂成本适配器、服务端模型环境与上传额度保留。截图和带合成数据的验收记录只保存在私有服务器构建目录，不作为真实客户报价发布。
