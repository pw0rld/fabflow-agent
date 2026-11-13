# DSH 工程拆图演示入口

这是 DSH 的 UI 插件入口。浏览器端使用 DSH 自带 React，向 `sidebar.footer.action` 添加“好运来工程拆图”按钮，向 `shell.overlay` 添加全屏工作台。工作台内容来自同源 `/workbench/`，可返回 DSH 或独立打开。

2026-10-24 已安装到 ZeroClave 的 web profile。工作台已更新到 `workbench-20261024-4` 的三步流程，完成实际 DSH 浏览器验收。刷新 DSH，点击侧栏底部“好运来工程拆图”，先进入文件上传首页，点击“查看演示样例”后查看“原始资料 → 拆件与补图 → 工程资料包”。

插件不注册 Agent 工具，不调用模型，不读写询价记录。演示逻辑和 SVG 仍由 `frontend/design/assembly-engineering-flow.html` 经构建转换 提供。本插件不复制整套工作台，不替换 DSH 聊天栏或侧栏。

工序工作台包含 8 类 24 项目录、3 项补充及 SS304/SPCC/孔加工三种模拟样例，可查看高亮作业图、辅料与工序汇总。当前工序 SVG/CSV 可下载，工时及费率仍待核定。

## 部署

适配服务器当前 DSH `0.1.1-rc.2` 已安装的两个扩展槽。需要先部署工作台，再将本目录的 `package.json`、`index.js`、`client.js` 和 `deploy/install_engineering_plugin_remote.py` 暂存到服务器。工作台静态版本更新不要求重复安装本入口插件。

在服务器执行 `python3 install_engineering_plugin_remote.py <暂存插件目录>`。安装程序会：

- 将插件安装在 `/opt/haoyunlai/dsh-engineering-demo`。
- 备份现有 web profile 配置和依赖清单到受限备份目录。
- 添加本地包映射和独立插件条目，保留既有 privacy 插件配置。
- 重启 DSH，确认首页启动图包含本插件、实际服务的 bundle 与源文件逐字一致。
- 若安装未通过，恢复配置、删除本次包映射及插件文件，再启动原 DSH。

安装不重新解析或升级 DSH 依赖。后续对 profile 执行 pnpm 操作时，应保留本地 `file:/opt/haoyunlai/dsh-engineering-demo` 依赖。服务器凭据、原 profile 配置和业务文件不进入源码备份。

## 验收

安装程序的成功回执只证明插件已被宿主发现和提供，不能代替浏览器验收。应在 DSH 页面刷新后点击侧栏底部入口，确认内嵌工作台可预览、下载样例和返回。当前独立 `/workbench/` 的线上验收见 `verification/workbench-assembly-live.json`；初版四页 Mock 的验收保留在归档，插件安装和浏览器验收分别记录。

插件实际浏览器验收见 `verification/dsh-engineering-plugin-browser.json`。验收通过管理员 SSH 转发连接 ZeroClave 实际服务，未使用本地界面替身。报告保留设置和凭据 describe 接口的 403：安装版 DSH 将这两个管理接口限制为 loopback authority，公开站点 Host 会被拒绝；本插件未修改这项策略。工作台及插件资源没有意外 HTTP 错误。

当前挂载状态应以 `verification/dsh-engineering-plugin-deployment.json` 为准；没有安装成功回执时，不宣称入口已经上线。
