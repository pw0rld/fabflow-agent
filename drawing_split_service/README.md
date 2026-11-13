# 原图自动分图 API

仅在 ZeroClave 运行，开发者电脑只编辑和同步。复用服务端既有 Pillow/OpenAI runtime，不借用样例参数或预设区域。

```sh
python -m drawing_split_service.server \
  --host 127.0.0.1 --port 3199 \
  --data-dir /var/lib/dsh/drawing-split \
  --allowed-origin https://8.135.55.251 \
  --public-base api/ \
  --adapter-module drawing_split_service.model_adapter
```

Nginx 的 `/ortho2cad-test/drawing-split/api/` 沿用 Demo 认证，以 `proxy_pass http://127.0.0.1:3199/` 剥离前缀。任务目录不可位于静态网站目录；部署单元限制 CPU、内存和停止超时。

- `POST api/jobs?name=filename.png`：原文件 bytes，`Content-Type: image/png` 等。必需 `X-Drawing-Split-Request: 1`、同源 `Origin` 和 `X-Request-Id`（UUID）。返回 202 及任务状态。
- `GET api/jobs/:id`：读取状态，不调用模型。返回 `job_id,status,stage,message,attempt,source,source_url,original_url,manifest,warnings,error,model,status_url,result_url`。
- `POST api/jobs/:id/retry`：JSON `{"request_id":"new-UUID"}`，同源头要求不变。显式产生新 attempt；同请求编号重放不增加模型调用，旧裁图保留。
- `GET api/jobs/:id/files/:registered_filename`：仅已登记并通过 SHA-256 的原图、规范 PNG、裁图及规范结果 JSON。模型原始返回/审计/任务记录私有，不提供任务清单。
- `GET api/health`：健康检查。

文件地址均相对 Demo 目录，为 `api/jobs/...`。`source_url` 从上传原件切换为已规范 EXIF 的 `canonical.png`，匹配区域坐标；`original_url` 永远指原始字节。每个状态都有可用的 `source_url`。

状态：`queued → running → succeeded | partial | failed`。重启将未完成任务设为 `interrupted`，不自动重发付费请求。`partial` 保留可用分区；读取参数未完成时不能当完整识别。

模型适配器 `create_adapter()` 返回有 `analyze(canonical_path,width,height,progress)` 的对象；可选 `model_info={provider,model}`。`progress(stage,message)` 可报告 locating/extracting/analyzing，页面采用服务固定阶段描述，不显示估算百分比或异常里的接口地址。

模型结果明确 `coordinate_system: normalized_1000 | source_pixels`，所有 bbox 为同一规范整图的 `[x0,y0,x1,y1]`。分类：orthographic、flat_pattern、isometric、process_table、technical_notes、title_block、detail。未知分类保留为局部详图；无效区域跳过并警告，不让未知业务字段使整轮失败。

manifest 包括 source、part/parts、regions、parameters、process_notes、bend_rows、relationships、warnings、review_notes、analysis_complete。裁片直接保留规范原像素，不放大重绘。参数/工艺说明/折弯行须带有效区域引用及原文，未知数值为 null，状态始终 pending_review。多视图不代表多零件或订单数量；制造数量不自动推定。

支持 JPEG、PNG、WebP、GIF，最多 20 MiB、单边 20,000 像素且总计不超过 2,400 万像素；验证魔数与完整解码，动画仅首帧并保留原件。目录 0700、文件 0600、原子 JSON 保存；请求编号仅对该次上传幂等，不按图片哈希跨客户共享任务。

无真实模型调用的测试仅在服务器执行：

```sh
PYTHONPATH=. python -m unittest discover -s tests -p 'test_drawing_split*.py' -v
```

## 模型与运行依赖

复用 DSH 现有 openai / gpt-6-sol 配置及服务器代理；密钥只在服务端读取，不另存到网页或发布包。Python 标准库负责 HTTP/任务管理，Pillow 验证和裁取原始像素，OpenAI SDK/httpx 调用 Responses，PyYAML 读取现有配置。当前复用已有服务端轻量依赖入口，未安装新的 CAD/OCR 工具。

一次任务最多两次模型请求：整页定位区域，然后结合整页与原像素裁片提取参数/折弯表。第二段失败时返回已定位分区及 partial。显式点击重试才创建新 attempt，SDK 关闭自动重试。页面刷新和恢复只 GET。

正投影区域在模型框外保留横向 2.5%、纵向 1.5% 整页上下文；其他区域各保留 0.6%。边界夹在原图内，这只是裁取周围原像素，并未补画图形或提高原始分辨率。预览透明图给模型时合成白底，上传原件与透明裁片保持原样。

任务与模型回执在 /var/lib/dsh/drawing-split/，目录700、文件600。仅裁片、原件和规范结果可经原有 DSH 认证访问。后台进度没有虚构百分比。重启不自动重发未完成请求。

本模块自动分图是图片区域分解及带来源的参数读取；没有建立完整孔位坐标系，不证明制造几何正确。PDF本轮仍只预览。视图方向和跨视图尺寸归属仍可能出错，所有结果保持 pending_review。

调用方式核对：[图像输入](https://developers.openai.com/api/docs/guides/images-vision?api-mode=responses)、[结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)。当前中转兼容路径采用 JSON 提示和服务端结构校验，不假设中转支持 strict JSON Schema。

## 参数校正、分区调整和资料包

`GET jobs/:id/review`读取当前轮次的有效分区/参数、revision与修订历史，适配旧任务；只读访问不改变原始AI结果。旧参数缺少方向、基准时保持unknown/null。仅根据名称得到的分类明确标记label_hint；重叠来源、多值项和缺失基准只是校验提示，不能据此自动改值或核对。

`POST jobs/:id/review`沿用同源保护，格式为 `{request_id,expected_revision,expected_attempt,action}`。action支持parameter/region/reset_parameter/reset_region，须指定现有ID。参数可修改名称、值、单位、分类、方向、基准、对象、关联分区、备注；状态pending_review/checked/questioned。确认仅覆盖当前单项，仍不代表工程批准。修改内容后自动恢复待核对；调整分区边界后，该区关联的checked参数也恢复待核对。

原始候选、原始依据和裁片保持不变。当前人工选择与original_candidate/original_evidence分开；人工重关联不把原文冒充为另一区的原始文字。逐项修订写入私有review-events及review.json，有旧值/新值/时间/当前页面操作者（身份未提供）。相同请求幂等，轮次/revision冲突返回409，重试模型的新轮次不继承旧确认。

`GET jobs/:id/export?revision=N&attempt=N`生成ZIP，包含原件、规范原图、当前分图、UTF8 BOM参数CSV、JSON及修订历史。JSON坐标对应source/canonical.png，原始上传字节另存source/source.ext；CSV对公式前缀作文字转义。包内engineering_approved=false。浏览器临时新增但未存服务端的手工区域不纳入服务端包，可单独下载或导出JSON。

本轮校正及导出不调用模型。最多256次修订、核对裁片累计256MiB、单个资料包192MiB；超限返回可读错误，不删除既有资料。接口与素材仍由同一个受保护Nginx路径提供。
