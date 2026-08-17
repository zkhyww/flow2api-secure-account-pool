# 影策／巨天视频能力兼容层设计

## 目标

让影策／巨天通过现有 `POST /v1/videos` 稳定调用 Flow2API 已真实验证的视频能力，同时让使用者在 Flow2API 中直接看到每个模型的生成方式、画幅、时长、清晰度和参考图边界，避免把巨天的通用菜单误认为模型真实能力。

## 已确认事实

- 巨天当前把自定义视频渠道统一显示为 `1-15s · 480P/720P/1080P/2160P`，没有使用 Flow2API 的逐模型能力范围。
- 巨天提交 Veo 3.1 Lite + 720P 和 Omni + 1080P 时，Flow2API 均在兼容入口返回 `unsupported_resolution`；请求没有进入账号池和上游生成。
- `C:\Users\Administrator\Downloads\extension.zip` 是旧版账号导入／reCAPTCHA Worker。它不包含 `/v1/videos`、模型映射、时长映射或分辨率映射，不是影策视频协议转换器。
- 现有 `src/api/yingce_adapter.py` 已是独立的薄协议适配层，并继续复用原有 `GenerationHandler`、账号池、并发控制和 `FileCache`。
- Google 官方能力表明确区分文生视频、首帧／首尾帧生视频、Ingredients／References 生视频、视频编辑和续写。当前 Flow2API 公开目录只列文生视频，不能据此推断底层模型不支持图片输入。
- Flow2API 底层已经存在 Omni Reference Images、Veo 3.1 I2V、Interpolation、R2V 和 Extend 路径；本次兼容层不得删除、覆盖或错误标成“不支持”。

## 不可破坏边界

- 不修改巨天／影策主程序。
- 不修改 `FlowClient`、`GenerationHandler` 的真实提交与轮询主路径。
- 不修改账号池、登录持久化、数据库、浏览器生命周期、验证码和并发调度。
- 不修改已通过的图片兼容接口及原有 `/v1/chat/completions` 合同。
- 不恢复浏览器插件依赖；插件仍不是视频协议适配层。
- 不把未真实验证的组合标成可用，也不静默把 1080P/4K 冒充成原生清晰度。
- 不用“当前公开菜单隐藏”替代“上游或底层不支持”的事实判断；每种生成方式分别记录官方支持、代码实现和真实验证状态。

## 官方能力、现有代码与公开状态

| 能力／方式 | Google 官方说明 | Flow2API 现有底层 | 当前公开目录 | 本次处理 |
|---|---|---|---|---|
| Omni 文生视频 | 双画幅，4／6／8／10 秒 | 已有 8／10 秒横竖入口 | 已公开 8／10 秒 | 保留并适配影策参数 |
| Omni Ingredients／References | 双画幅，4／6／8／10 秒，并支持高级角色／声音参考 | `omni` 已有 0–3 张参考图的 8 秒 Reference Images 路径；10 秒当前仍是纯文本入口 | 被错误隐藏 | 先公开现有 8 秒图片参考能力；更长时长按真实测试逐项提升 |
| Veo 首帧／首尾帧生视频 | 双画幅，4／6／8 秒 | 已有 I2V、Lite I2V 与 Interpolation 路径；支持 1 张首帧或 2 张首尾帧 | 被隐藏 | 以独立生成方式公开，先保留用户要求的最新版 8 秒入口 |
| Veo Ingredients／References | Lite／Fast 双画幅、8 秒；Quality 官方不支持该方式 | 已有 Veo 3.1 R2V Fast，多参考图上限 3 张 | 被隐藏 | 作为独立“参考图生视频”能力公开，不冒充 Quality |
| Veo 文生视频 | 双画幅，4／6／8 秒 | Lite／Fast／Quality 横竖入口均存在 | 当前只公开 8 秒 | 保留当前最新版 8 秒公开策略 |
| Extend | Veo 3.1 的 8 秒结果可续写，实际使用 Lite 续写入口 | 已有横竖 Extend 路径 | 已作为成功结果后的动作 | 保持结果动作，不伪装成普通模型 |

所有上述能力都必须把“官方支持”“代码存在”“本地真实验证”分开标注。1080P、4K 和其他组合只有在对应模型完成真实生成、媒体存在、解码参数符合预期后，才能加入本地 `validated` 能力表。代码中存在候选别名或放大路径不等于本地已经验收。

## 架构与数据流

保留单一调用链：

1. 影策／巨天调用现有 `/v1/videos`。
2. `yingce_adapter.py` 读取服务端公开能力表，完成模型别名、画幅、时长和清晰度兼容写法归一化。
3. 合法组合解析为现有已验证模型 ID，并交给原有 `GenerationHandler`。
4. 不合法组合在进入账号池前返回稳定 4xx，同时给出该模型可选的画幅、时长、清晰度和参考图范围。
5. 真实生成、账号选择、并发、验证码、轮询和媒体缓存继续完全走原路径。

`src/main.py` 不增加新的 router、进程、端口或生命周期对象；现有适配 router 已经注册，本次只修改适配层内部行为。

## 能力元数据

Flow2API 的视频模型元数据补齐以下事实：

- `generation_modes`：分别列出文生视频、首帧生视频、首尾帧生视频、Ingredients／References 生视频；Extend 为成功结果后的动作。
- `supports_images`、`min_images`、`max_images`。
- 每种图片输入方式的语义和张数，避免把“首尾帧”与“人物／素材参考图”混成同一种请求。
- `options.aspect_ratio`。
- `options.duration_seconds`。
- `options.resolution`：公开值为 `native`，并注明影策／巨天提交时选择 `720P`。
- `usage_guide`：一句大白话说明“怎么选”。
- `unsupported_notes`：只列真实不支持或尚未完成本地验证的组合，不能把已存在的 I2V／R2V 路径写成模型不支持。

内置测试页在模型选择后直接显示上述说明。`GET /v1/models` 与测试模型接口继续共用同一份服务端目录，不复制第二份硬编码说明。

## 兼容归一化规则

- `720p`、`720P`、空清晰度和 `native` 统一解析为“使用该模型上游原生输出”，不触发放大。
- `size` 只接受当前能力表中的 16:9／9:16 及其现有像素或 landscape／portrait 等价写法。
- 时长只接受能力表公开值：Omni 8／10 秒，Veo Lite／Fast／Quality 8 秒。
- 模型 ID 和 capability ID 均通过公开目录解析，不在适配器另建第二份模型表。
- 无图片走文生视频；Omni 8 秒带图走现有 Reference Images 路径。
- Veo 的首帧、首尾帧和 Ingredients／References 使用不同 capability／generation mode 明确表达，不只根据“上传了几张图”猜用途。
- 1080P、4K、2160P、480P 或其他未公开组合返回 `unsupported_video_parameters`，错误消息带安全、简短的可选范围。
- 图片数量、用途或模型方式不匹配时继续 fail-closed，并返回对应方式的正确选择；不得把参考图静默丢弃后改做文生视频。

## 错误展示

巨天应能看到类似下列大白话错误，而不是笼统的 `Unsupported video resolution`：

> Omni Flash 当前可选：文生视频 8／10 秒；参考图生视频 8 秒、最多 3 张；均支持 16:9／9:16 和原生清晰度（影策／巨天请选择 720P）。10 秒参考图仍需完成本地真实验证。

公开错误不得包含提示词、账号、Token、Cookie、API Key、上游 URL、原始响应或内部异常。

## 测试与验收

按 TDD 先新增失败合同，再做最小实现：

1. Omni 8／10 秒文生视频 + 16:9／9:16 + `720P` 能进入既有 handler，并映射为正确模型 ID。
2. Omni 8 秒上传 1–3 张参考图时进入现有 Reference Images 路径；10 秒参考图在未验证前给出明确边界，不丢图、不伪装成文生。
3. Veo Lite／Fast／Quality 8 秒文生视频 + `720P` 能进入既有 handler。
4. Veo 8 秒首帧、首尾帧和 Fast Ingredients／References 能映射到各自已有 I2V／Interpolation／R2V 路径；Quality 不错误宣称支持 Ingredients／References。
5. Omni + 1080P、Veo Lite + 1080P、任意公开模型 + 480P／2160P 在提交前明确拒绝，不产生账号 reservation 或上游调用。
6. 不支持或尚未验证的时长、画幅、清晰度、图片数量和图片用途返回具体支持范围。
7. 模型元数据和测试页分别显示文生、首帧、首尾帧、Ingredients／References、续写、画幅、时长、原生清晰度说明和验证状态。
8. 图片接口、`/v1/chat/completions`、账号池和现有模型目录回归不变。
9. 运行聚焦 pytest、全量 pytest、`compileall`、`git diff --check` 和秘密扫描。
10. 自动测试通过后，从巨天真实验证 Omni 文生、Omni 参考图生、一个 Veo 文生、一个 Veo 首帧／首尾帧以及 Veo Fast 参考图生；验证任务完成、媒体可播放、没有新增登录误判，并确认浏览器按既有策略回收。

## 交付边界

本次只形成 Flow2API 内部独立兼容层补丁、能力说明和回归测试。若未来要让巨天自动隐藏菜单中的无效选项，需要巨天主动消费 Flow2API 的能力元数据，属于独立后续集成，不在本次补丁中偷偷修改。
