# 影策（Framefield Studio）Flow2API 兼容适配层设计

## 目标与边界

在 Flow2API 现有 `POST /v1/chat/completions` 统一生成能力前增加两个薄协议适配面：

- OpenAI Images：`POST /v1/images/generations` 与 `POST /v1/images/edits`。
- OpenAI/NewAPI Videos：`POST /v1/videos`、`GET /v1/videos/{id}`、`GET /v1/videos/{id}/content`。

适配层必须继续使用同一总 API Key、同一 `GenerationHandler`、同一账号池、同一并发控制、同一幂等与错误归因、同一 `FileCache`。不新增账号池、上游客户端、凭据存储、外部队列或数据库表，不改变 `/v1/models` 和 `/v1/chat/completions` 的既有合同。

## 架构

新增 `src/api/yingce_adapter.py`，只负责 HTTP 协议解析、模型参数归一化、调用既有 generation handler、把 chat completion 媒体结果转换为 Images/Videos 响应，以及把媒体交付限定为本地安全 URL/文件流。

新增 `src/services/compat_video_tasks.py`，提供进程内有界视频任务注册表。注册表保存任务 ID、公开模型、状态、进度、时间戳、稳定错误类和本地缓存文件名；不保存 API Key、提示词、参考图内容、账号信息或上游 URL。

`src/main.py` 只增加适配 router 和现有 `GenerationHandler` 的注入，不改变生命周期、账号池或浏览器服务初始化。

## Images 数据流

1. FastAPI 现有 `verify_api_key_flexible` 校验 Authorization Bearer。
2. JSON generations 校验 `model`、非空 `prompt`、`n=1`；`size` 与 `quality` 交给现有 resolver 映射。
3. multipart edits 读取 `model`、`prompt` 和一个或多个 `image` 文件。若出现 `mask`，立即返回明确 4xx，且不得调用生成 handler。
4. 调用现有 `GenerationHandler.handle_generation(..., stream=False)`。
5. 从既有 chat completion 结果提取媒体；若是上游地址或 data URL，使用现有 `FileCache` 落盘；只返回 `{created, data:[{url}]}`，URL 指向本服务 `/tmp/<filename>`。
6. handler 错误只保留稳定错误类和通用消息，不回显提示词、密钥、上游 URL 或原始异常文本。

## Videos 数据流

1. `POST /v1/videos` 解析 multipart 的 `model`、`prompt`、`seconds`、`size`、`resolution_name`、`preset` 与 `input_reference[]`。
2. 依据现有 resolver 和公开目录映射横竖屏、时长与可调用分辨率；未知模型、错误媒体类型或当前能力不支持的时长返回 4xx。
3. 规范化请求生成不可逆 SHA-256 指纹。存在 `Idempotency-Key` 时，同键同指纹复用任务 ID；同键不同指纹返回 409。
4. 注册表先创建 `queued` 任务，再用 `asyncio.create_task` 调用既有 handler。创建接口不等待真实生成结束。
5. 后台任务进入 `in_progress`，收集既有 handler 结果。成功媒体始终通过现有 `FileCache` 转成本地缓存文件名，任务转为 `completed`；失败只记录稳定错误类和通用消息。
6. `GET /v1/videos/{id}` 返回 OpenAI video object：`id/object/model/status/progress/created_at/completed_at/expires_at/size/seconds/error/url`。完成时 `url` 指向本服务 `/v1/videos/{id}/content`。
7. `GET /v1/videos/{id}/content` 只读取注册表中的本地文件名，并用 `FileResponse` 流式返回；未完成返回 409，失败返回稳定错误，过期/不存在返回 404。绝不重定向到上游。

## 生命周期与容量

默认任务 TTL 为 2 小时，默认容量为 256。清理在 create/get/update 前惰性执行：

- 只清理已经 `completed` 或 `failed` 且超过 TTL 的任务。
- 容量不足时先淘汰最旧终态任务。
- 若容量仍被全部活动任务占满，创建返回 503，不删除活动任务。
- 清理任务时同步清理 Idempotency-Key 索引。
- 正在运行的 asyncio task 由适配器集合持有强引用，完成后自动移除。

## 模型与参数

- 图片公开能力仍为 Nano Banana 2 与 Nano Banana Pro；适配器不改 `/v1/models`。
- 视频公开能力仍为 Omni Flash、Veo 3.1 Lite、Fast、Quality。
- Veo 三种公开能力默认/支持 8 秒；Omni 支持目录已验证的 8 秒与 10 秒。
- `size` 用现有 resolver 决定横竖屏；`resolution_name` 只在现有 resolver/模型配置存在对应可调用模型时映射。
- `preset` 被接受为兼容字段，但不覆盖调用方明确选择的模型，不另造模型。
- `input_reference[]` 作为参考图 bytes 传给现有 handler；不新增上传逻辑。

## 错误与隐私

- 所有五个新端点都使用现有 API Key 依赖，缺失或错误返回 401。
- mask 不支持时返回 400，错误码 `mask_not_supported`。
- handler 失败映射为稳定 error code；不返回原始异常、完整上游 URL、提示词、密钥、账号邮箱、Cookie 或 Token。
- 适配层日志只允许状态、稳定错误类、是否有媒体、时长、数量和资源终态；本实现不记录请求正文或媒体地址。
- 任务 ID 为随机或幂等键派生的不可逆标识，不包含密钥和提示词。

## 测试与验收

先新增 HTTP 合同测试并确认端点缺失导致 RED，再最小实现到 GREEN。覆盖：

- 鉴权 401；
- Images JSON 成功形状与参数映射；
- Images multipart 参考图；
- mask fail-closed；
- Videos create → poll → completed → content；
- failed 状态与稳定错误；
- Idempotency-Key 同键只提交一次、冲突 409；
- TTL 与容量边界；
- 响应和捕获日志不出现测试密钥、测试提示词或模拟上游 URL；
- `/v1/models` 精确六能力/2 图 4 视频回归；
- `/v1/chat/completions` 参数和响应回归。

自动门禁为聚焦 pytest、全量 pytest、`compileall`、静态 JavaScript parse（仅在相关页面脚本改变时）、`git diff --check` 和敏感信息/禁止路径静态扫描。真实 Flow 生成留给 Codex 独立验收。
