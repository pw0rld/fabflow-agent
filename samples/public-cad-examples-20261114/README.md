# 官网公开 CAD 样例来源

官网默认展示公开样例；客户文件只在显式进入个人上传流程后，按原有浏览器会话读取。样例访问不提交上传任务、不扣额度、不调用模型。

## 本目录新增原件

法兰衬套由 CADProps 原创建模，按 CC0-1.0 公开。固定版本 `cfffc2facc1700e763149f979efdd65ee73751ed`。

- [STEP 与逐件说明](https://github.com/cadprobs-a11y/cadprops-step-models/tree/cfffc2facc1700e763149f979efdd65ee73751ed/parts/sleeves-and-washers/flanged-bushing)
- [许可适用范围](https://github.com/cadprobs-a11y/cadprops-step-models/blob/cfffc2facc1700e763149f979efdd65ee73751ed/LICENSE.md)
- 原件：`flanged-bushing.step`，14,656 bytes。
- SHA-256：`ff1c4f89904db8655a32e4714a3a2cfba4a8490c4ea7c774805bd3a7a67e9b9d`。
- 原始许可、逐件 metadata 和 README 一并保留；没有执行上游生成程序。

同仓库的社区零件不因此变为 CC0。本次只选逐件记录明确属于 CADProps 原创的法兰衬套。

## 另外两例

- 电机支架复用 `samples/cad-test-20261019/step` 中固定版本的 FreeCAD-library 模型，保留 CC-BY-3.0 来源和贡献者署名，不把提交者冒充原作者。
- 钣金机箱复用 `samples/public-enclosure-20261026` 的 Laser4DIY 原件，固定原始提交、版权和 CERN-OHL-P v2 许可。

`tools/build_public_examples.py` 只接受这三个固定来源哈希，在服务器复用现有 OCCT 原生读取器，生成独立的 `web/public-examples`。原 STEP 字节不变；网格、原定义分组与参数是派生数据。模拟报价使用公开列明的统一假设，不引用客户报价、工厂价目表或供应商成交价，也不代表已完成工艺审核。
