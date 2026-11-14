# 体验页品牌与后续迁移

目前先在 ZeroClave 提供体验，不修改或部署 `willflowai.com` 官网。后续迁移到正式环境时复用这一套代码和部署规则。

## 视觉约定

2026-11-14 核对线上官网的实际 HTML 和 CSS（`styles.css?v=4569b1d175`）。本地 `willflow-website` 的旧浅色版本不是本轮依据。

官网品牌用于导航：深色 `#070b17`、官方渐变 W 形标志、“意志流动 / WILLFLOW.AI”字标，以及青蓝紫主按钮。工作区采用雾白背景、白色数据面板、中性浅灰 3D 视窗，适合查看图纸、参数和表格；不将整套工程工具改为纯黑。源图和打印报价仍保持原有纸张呈现。

- 共享品牌：`frontend/components/willflow-brand.tsx`、对应 CSS 和 `frontend/public/willflow-mark.svg`。
- 公共样例主题：`frontend/components/public-cad-examples.css`。
- 个人体验主题：`frontend/components/willflow-workspace-theme.css`，限定在 `.fft` 内，不影响内部后台或原始报价文档。
- 体验页导航返回官网；“用我的 STEP 体验”仍进入 `/try/?mode=upload`。默认 `/try/` 仍只加载公开样例，不恢复客户历史。

## 迁移时保留

API、样例与静态资源沿用同源相对路径；品牌链接集中在共享组件。更换站点域名不需要将服务器 IP 写入前端。

新环境须复用 `/try/` 的精确页面代理、`/workbench/_next/` 和 CAD 查看器资源、公共样例的严格只读白名单、客户 API 的会话隔离，以及内部线索页的认证保护。非标准端口预览须正确传递请求 Host 与 Origin，不能关闭同源检查。

构建沿用 `HYL_DEMO_BASE_PATH=/workbench`。如同时变更这些路由前缀，需配套调整前端 API 常量及 Nginx 规则，不能只改资源前缀。运行环境的模型凭据、订单、线索数据库、工厂价格与额度账本始终独立保存；迁移时做私有备份，不放入公开源码或示例目录。
