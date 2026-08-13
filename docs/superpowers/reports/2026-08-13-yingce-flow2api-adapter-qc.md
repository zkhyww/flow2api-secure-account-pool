# Flow2API 影策兼容 API 适配层 QC

日期：2026-08-13

## 结论

本轮在 `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo` 的既有脏工作树上完成影策（Framefield Studio）OpenAI Images + NewAPI Videos 薄兼容层。没有建立第二套上游客户端、账号池、密钥体系、模型目录或持久任务队列；图片/视频提交继续复用现有 `GenerationHandler`、账号池、并发控制与 `FileCache`。

本轮没有执行真实生成、服务重启、commit、push、PR、发布或部署，也没有读取或输出真实 Cookie、Profile、Token、API Key 值、账号邮箱、原始提示词、真实媒体、真实响应正文或完整真实上游/媒体 URL。

## Governed 起点

- worktree：`D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo`
- branch：`main`
- opening HEAD：`2b9856088d47b2d29f77bc077e2340482d67e734`
- opening 状态：仓库已有大量用户 dirty，本轮不清理、不覆盖、不 reset/clean/stage/commit。
- 设计基线：`docs/superpowers/specs/2026-08-13-yingce-flow2api-adapter-design.md`

## 实现范围

- Images generations：JSON `model/prompt/n=1/size/quality`，走现有 resolver 和 handler，结果优先 `b64_json`。
- Images edits：multipart 参考图复用 handler；mask 直接 `400 mask_not_supported` 且不提交。
- Videos：`POST /v1/videos` 创建任务，`GET /v1/videos/{id}` 查询，`GET /v1/videos/{id}/content` 本地代理二进制。
- 五个新端点全部复用现有 `verify_api_key_flexible`；没有第二套 API Key。
- 视频公开状态仅 `queued/in_progress/completed/failed/cancelled`；任务表默认 TTL=7200 秒、容量=256，活动任务不因容量淘汰。
- `Idempotency-Key` 只保存 SHA-256 摘要与请求指纹；同键同请求复用 task，同键异请求 409。
- `resolution_name` 只映射现有 `MODEL_CONFIG` 中真实存在且带 upsample 的 callable model；`preset` 只作兼容字段。
- 远端媒体继续复用 `FileCache`。新增默认仍为 True 的 `log_source_url` 可选参数；只有影策适配器传 False，旧调用行为不变。

## RED → GREEN 证据

1. 鉴权 RED：五个新路径均返回 404，而合同要求复用现有密钥先返回 401；最小路由接入后 `1 passed, 5 subtests passed`。
2. Images JSON RED：`/v1/images/generations` 返回 501；实现 resolver/handler 映射与 OpenAI Images data 后 GREEN。
3. Images edits RED：参考图与 mask 两条均 501；实现 multipart 参考图和 mask fail-closed 后 `2 passed`。
4. Videos 基础 RED：create 返回 501；实现 task/create/poll/content 后 GREEN。
5. 幂等与稳定错误 RED：同 key 产生两个 task，未知 provider code 被原样返回；最小 GREEN 后 `2 passed`。
6. TTL/容量 RED：`2 failed, 1 passed`，分别证明过期终态未清理、容量满未淘汰终态；修复后 `3 passed`。
7. 媒体交付 RED：本地图片仍返回 URL、远端视频未缓存，`2 failed`；修复后 `2 passed`。data-URL 视频另有 `1 failed` → `1 passed`。
8. failed content RED：返回 `video_not_ready` 而不是任务稳定失败码；修复后 `1 passed`。
9. resolution RED：Veo Quality 8 秒 1080p 被错误拒绝；按现有 MODEL_CONFIG 精确映射后 `1 passed`，Omni 同参数继续 fail-closed。
10. 异常边界 RED：畸形 provider status 抛 ValueError，FileCache 异常使 task 卡 `in_progress`，共 `2 failed`；修复后 `2 passed`。
11. FileCache 隐私 RED：`log_source_url=False` 触发 unexpected keyword TypeError；加入默认兼容参数并由影策显式关闭来源 URL 日志后 `1 passed`。

## Fresh gates

- 影策聚焦（最终 fresh）：`21 passed, 5 subtests passed in 1.04s`。
- 全量 pytest：`334 passed, 99 subtests passed in 101.26s`。
- `venv/Scripts/python.exe -m compileall -q src tests scripts`：exit 0。
- 页面 JavaScript：本任务没有修改页面/JS，parse 门禁不适用。
- `git diff --check`：exit 0；仅有工作树既有 LF/CRLF 转换 warning，没有 whitespace error。

## 隐私与安全检查

- mask、当前公开 T2V 参考图不支持路径均在 handler 调用前 fail-closed；对应 synthetic 合同断言提交次数为 0。
- poll/content 不包含 provider 原始 detail、原始提示词、API Key 或远端媒体地址。
- `/content` 只接受注册表中的安全 basename；`../outside.mp4` synthetic 路径穿越合同返回 404，未读取缓存目录外文件。
- 远端图片/视频使用现有 FileCache 时由影策显式 `log_source_url=False`；真实 FileCache synthetic 测试证明来源 URL 不进入 info log。
- 本轮测试只使用 synthetic 文本、bytes、临时目录和 fake handler/session，没有真实 Flow 请求。

## 已知边界

- 视频 task 注册表是进程内内存状态；服务重启后旧兼容 task id 不恢复，这是本设计的已知边界，不伪装为 durable queue。
- mask 当前明确不支持；当前公开 Omni/Veo T2V 能力也不声称支持参考图，传参考图会 fail-closed。
- 本轮未做真实影策客户端 UAT、真实生成或部署；自动合同和全量回归已闭环，真实 UAT 仍应由主控在允许的环境中独立执行。
