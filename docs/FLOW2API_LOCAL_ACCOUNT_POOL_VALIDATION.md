# Flow2API 本地账号池与公开能力验收报告

日期：2026-08-13

## 本轮结论

本轮在现有脏工作树原地完成公开模型目录与测试页参数化重构，没有建立第二套模型路由，也没有删除底层兼容别名。

- `/v1/models`、`/api/test/models` 与测试页共用 `src/core/public_model_catalog.py`，公开 2 个图片能力与 4 个视频能力。
- 图片模型、比例和清晰度独立选择；视频模型、比例和时长按 capability metadata 独立选择。Omni Flash 可选 8/10 秒且默认 10 秒，Veo 3.1 Lite/Fast/Quality 仍固定为 8 秒。
- 参数组合由纯解析函数映射回现有兼容 model ID；旧组合 ID 和旧别名仍可直接调用，但不进入发现目录。
- Extend 只作为成功视频后的“续写此视频”动作存在，不是普通可选模型；源标识只保存在页面内存，不进入日志、可见 DOM 文本或本报告。
- metadata 支持 `validated`、`membership_required`、`hidden`。正式页面只列可选状态，未来标为 `hidden` 的组合不会进入参数菜单。
- Nano Banana 2 与 Nano Banana Pro 默认均为 1K；1K/2K 显示“可用”，4K 显示“需要高级会员”且不默认选择。
- Omni Flash 当前正式 capability 是 0–0 上传限制的文生视频入口；说明文案不声称支持参考图。其横竖 10 秒入口已提升为 `validated`，现有横竖 8 秒入口继续保留。
- 管理会话专用 hidden 诊断机制继续保留；当前目录已无 hidden 映射，因此诊断列表为空且控件自动隐藏。
- 诊断账号、无 API Key 默认会话、高级 API Key、SSE、上传、结果预览、公开错误归因与隐私白名单保持原有合同。

## TDD 证据

初始目录与页面 RED：

- 命令：`venv/Scripts/python.exe -m pytest -q tests/test_canonical_model_catalog.py tests/test_test_page_contract.py`
- 结果：`13 failed, 5 passed`。
- 失败原因：目录仍枚举 10 个视频组合与全部图片组合；Extend 仍是普通模型；缺少六能力 metadata、图片/视频参数控件、纯解析器和隐藏组合过滤。

最终图片矩阵注入 RED：

- 结果：`2 failed, 17 passed`。
- 失败原因：Nano Banana Pro 仍被整体标为会员限制，4K 页面文案仍是非确定表述。
- 随最终 30 项白名单更新，Nano Banana Pro 的 3:4 / 2K 也提升为 `validated`。

Omni Flash 独立 UI QC RED：

- 结果：`1 failed, 7 deselected`。
- 失败原因：说明错误声称参考图能力，且 capability 上传合同需要收口为 0–0。

Omni Flash 10 秒正式开放 RED：

- 命令：`venv/Scripts/python.exe -m pytest -q tests/test_test_page_contract.py tests/test_canonical_model_catalog.py tests/test_omni_10s_support.py tests/test_admin_test_capability.py`
- 结果：`7 failed, 31 passed, 2 subtests passed`。
- 失败原因：Omni 默认仍为 8 秒、10 秒仍标 hidden、普通目录未公开 10 秒、页面时长仍写死 8 秒，且缺少有效值优先/默认值回退函数。

最终聚焦 GREEN：

- `tests/test_canonical_model_catalog.py tests/test_test_page_contract.py`：`21 passed`。
- Omni Flash 文案/上传限制单测：`1 passed, 7 deselected`。
- Omni Flash 10 秒正式开放四文件聚焦：`38 passed, 2 subtests passed`。

## Codex 真实 Flow 白名单

以下只记录用户提供的状态白名单。本轮代码 Owner 没有执行真实 Flow，也没有读取或输出 Cookie、Token、邮箱、完整 URL、请求响应、原始提示词、源视频标识或媒体内容。

### 图片：2 能力 × 5 比例 × 3 清晰度

比例集合：16:9、9:16、1:1、4:3、3:4。

| 能力 / 兼容前缀 | 1K | 2K | 4K |
|---|---|---|---|
| Nano Banana 2 / `gemini-3.1-flash-image` | 五比例全部 completed、has_media=true、`validated` | 五比例全部 completed、has_media=true、`validated` | 五比例全部 failed、has_media=false、公开 error_class=`upstream_error`；按已知账号级别限制归为 `membership_required` |
| Nano Banana Pro / `gemini-3.0-pro-image` | 五比例全部 completed、has_media=true、`validated` | 五比例全部 completed、has_media=true、`validated` | 五比例全部 failed、has_media=false、公开 error_class=`upstream_error`；按已知账号级别限制归为 `membership_required` |

上述 30 项均已逐项提交完成；4K 不写作普通可用，也不作为默认值。

### 视频与 Extend

| 公开能力 | 比例 | 时长 | 状态 |
|---|---|---:|---|
| Omni Flash | 16:9、9:16 | 8 秒 | 两入口均 `validated` |
| Omni Flash | 16:9 | 10 秒 | completed、has_media=true、decoded_duration=10.005、`validated` |
| Omni Flash | 9:16 | 10 秒 | completed、has_media=true、decoded_duration=10.005、`validated` |
| Veo 3.1 Lite | 16:9、9:16 | 8 秒 | 两入口均 `validated` |
| Veo 3.1 Fast | 16:9、9:16 | 8 秒 | 两入口均 `validated` |
| Veo 3.1 Quality | 16:9、9:16 | 8 秒 | 两入口均 `validated` |
| Extend 动作 | 横版、竖版 | 成功视频后续写 | 两入口均 `validated` |

### 账号池与无插件 onboarding

- 本地记录为 3 个账号，其中 2 个 active、1 个 inactive；未启用第三个账号。
- 两个 active 账号并发完成后，两账号 reservation=0、image/video inflight=0，页面均空闲。
- 无插件“添加账号”已真实启动独立浏览器并进入 `waiting_login`；Google 登录和验证码由用户继续完成。

## Batch 7：离线合成压力与恢复量化

- RED：`tests/test_batch7_synthetic_stress.py` 为 `3 failed in 0.11s`；失败只指向缺少离线压力夹具和严格白名单量化报告。
- GREEN：Batch 7 聚焦为 `3 passed in 39.76s`；既有调度、恢复、配额、浏览器生命周期、分页与 Batch 7 联合聚焦为 `50 passed, 6 subtests passed in 44.64s`。
- 夹具只使用临时数据库、假账号与 fake browser runtime；默认不联网、不读真实账号或凭据、不启动 Chrome、不执行真实 Flow。
- 覆盖 0/1/200/500 假账号、3/5/10 worker、dense-pack、超限排队、429 冷却、释放异常路径、并发幂等与模拟重启恢复。
- 18 条采样记录全部为 failure_count=0、leak_count=0、duplicate_submit_count=0、error_attribution_accuracy=1.0。10 worker 生命周期样本的 max_live_workers=10、queued_count=1；500 账号 API 分页样本 p95_ms=5.535、p99_ms=6.411、peak_memory_bytes=178409。
- 功能断言与性能采样分离；本次耗时和内存是环境快照，不作为极窄绝对时间门槛。
- 完整聚合数据见 `docs/validation/2026-08-13-synthetic-account-pool-stress-validation.json`，说明见同名 `.md`。

## Fresh 门禁

- 原目录聚焦 pytest：`21 passed in 0.95s`。
- Omni Flash 10 秒正式开放聚焦 pytest：`38 passed, 2 subtests passed in 1.48s`。
- 本轮 Batch 7 聚焦 pytest：`3 passed in 39.76s`。
- 本轮相关联合聚焦 pytest：`50 passed, 6 subtests passed in 44.64s`。
- 本轮全量 pytest：`297 passed, 78 subtests passed in 81.11s`。
- `venv/Scripts/python.exe -m compileall -q src tests scripts`：通过。
- 整页 JavaScript 的既有解析门禁保持通过；本轮相关 `static/test-page-capabilities.js` 通过 `node --check`。
- 本轮新增压力资产 credential-pattern scan：0 命中；只报告计数，未输出匹配内容。
- `git diff --check`：通过；仅有既有 LF/CRLF 转换提示。

## 已验证恢复证据与边界

- `docs/validation/2026-08-12-pluginless-account-persistence-validation.md` 已记录真实服务进程重启、加密数据库重载后成功执行，以及服务继续存活时空闲 80 秒后浏览器子进程归零。
- 本轮合成生命周期进一步证明 fake runtime 从 0 开始，压力触发 3/5/10 worker、超限任务排队、完成并 idle 后归零、新任务自动再唤醒；该结论不冒充真实 Chrome 或真实 10 并发验收。
- Batch 7 执行当时尚未做 Windows 整机重启、长期无人值守、部署或 Linux/Zeabur 验收；后续 Batch 10 已补齐 Windows 整机重启恢复。长期无人值守、部署与 Linux/Zeabur 仍未验收。真实 idle 后再唤醒和真实 Flow 压力已在 Batch 8 补齐；10 路图片属于峰值边界，不是 100% 稳定档。
- 本轮没有重启服务或 Windows，没有 commit、push、PR、Issue 或部署，没有修改 `D:\codex项目\Framefield-Studio`。

## Batch 8：真实图片并发压力

真实压力使用现有 5 个 active 账号、Nano Banana 2、16:9、1K 和无敏感内容。诊断只记录并发档位、状态、错误类别、是否有媒体、耗时与资源终态；没有保存账号、提示词、媒体地址、任务标识、请求响应或凭据。

| 并发档位 | 结果 | 批次收敛耗时 | 资源终态 |
|---:|---|---:|---|
| 3 | 3 completed、3 has_media | 约 85 秒 | 5 个账号 reservation=0、image/video inflight=0、browser occupancy=0/0 |
| 5 | 5 completed、5 has_media | 约 103 秒 | 5 个账号 reservation=0、image/video inflight=0、browser occupancy=0/0 |
| 10 | 8 completed/has_media、1 `rate_limited`、1 `upstream_error` | 约 212 秒 | 5 个账号 reservation=0、image/video inflight=0、browser occupancy=0/0 |

- 10 路失败任务未重提；所有账号保持 active，没有出现 reservation、inflight 或浏览器占用泄漏。
- 结论：5 路是当前真实全成功基线；10 路能够并行处理并最终收敛，但会出现上游限流/失败，不能写成 100% 稳定档。
- 两次浏览器控制超时发生在批量准备/点击阶段，经完成计数与资源状态核对均未进入 Flow，因此没有重复扣费；最终有效 10 路由两组 5 路在不足 1 秒内提交。
- 真实视频压力已在冷却后执行 3 路 Omni Flash、16:9、10 秒小批次：3/3 completed，3/3 均存在 video 媒体元素，单路约 59.7–72.6 秒。终态核对时 5 个账号均为 reservation=0、image/video inflight=0、browser occupancy=0/0、status=ok。

## Batch 9：真实 idle 回收与自动再唤醒

- 起点：Flow2API 服务继续运行，后代浏览器进程数为 0。
- 提交：1 路 Nano Banana 2、16:9、1K、无敏感内容图片；任务提交后观测到浏览器从 0 自动拉起。
- 结果：completed、has_media=true，页面记录生成耗时 37.9 秒。
- 资源：完成后 5 个账号均为 reservation=0、image/video inflight=0、browser occupancy=0/0、status=ok。
- 回收：再等待 80 秒后，Flow2API 后代浏览器进程重新归零。
- 结论：真实“idle 归零 → 新任务自动唤醒 → 生成完成 → idle 再归零”已闭环；不是合成测试。

## Batch 10：Windows 整机重启恢复

- Windows 整机重启后，8000 端口未监听；系统启动项和计划任务均未发现 Flow2API。结论是服务开机自启尚未配置，不能冒充为自动启动成功。
- 按项目现有 `python main.py` 方式手动启动服务后，5 个既有 Google 账号全部自动恢复为 active/ok，无需插件、配对或重新登录；启动时浏览器进程为 0。
- 提交 1 路 Nano Banana 2、16:9、1K、无敏感内容图片后，浏览器从 0 自动唤醒；约 47 秒 completed、has_media=true。
- 再等待 80 秒后，Flow2API 后代浏览器进程重新归零。
- 结论：Windows 重启后的 Google 登录态持久化、真实生成、按需唤醒与 idle 回收均通过；Batch 10 当时唯一未闭环的是 Flow2API 服务本身的开机自启配置。后续自启代码与当前真实 UI 发现见下方 2026-08-14 返修记录。

## 影策（Framefield Studio）兼容 API：Synthetic TDD 验收

本轮只新增协议适配层，没有执行真实图片/视频生成，没有读取或记录 Cookie、Profile、Token、API Key 值、账号邮箱、真实提示词、真实媒体或完整上游/媒体 URL，也没有重启服务、发布或部署。

- 新增 `POST /v1/images/generations`：复用现有 `GenerationHandler`，支持 `model/prompt/n=1/size/quality`，OpenAI Images `data` 数组优先返回 `b64_json`。
- 新增 `POST /v1/images/edits`：multipart 参考图 bytes 继续交给现有 handler；`mask` 当前不能保证语义时直接 `400 mask_not_supported`，且 synthetic 合同证明 handler 0 次提交。
- 新增 `POST /v1/videos`、`GET /v1/videos/{id}`、`GET /v1/videos/{id}/content`：创建立即返回 task id，后台复用现有 handler；公开状态只使用 `queued/in_progress/completed/failed/cancelled`，完成媒体只从本地 FileCache 代理二进制。
- 视频任务注册表默认 TTL=7200 秒、容量=256；create/get/update 前惰性清理终态，到容量上限时只淘汰最老终态，活动任务全部占满时返回 503。
- `Idempotency-Key` 只保存 SHA-256 摘要和请求指纹；同键同请求复用 task，同键不同请求返回 409，不把原始 key 或 prompt 存入任务表。
- `resolution_name` 只映射现有 `MODEL_CONFIG` 中真实存在且带 upsample 的 callable model；例如 Veo 3.1 Quality 8 秒 1080p 可映射，Omni 同参数 fail-closed。`preset` 只作为兼容字段接受。
- provider 错误、FileCache 失败和畸形 status 都转为稳定错误；poll/content 不回显原始异常、提示词、密钥或上游媒体地址。路径穿越 synthetic 合同证明 `/content` 不会读取缓存目录外文件。影策视频需要下载远端媒体时显式关闭来源 URL info 日志；显式 `http://` 媒体代理会使用预检公网 IP 的安全 CONNECT 隧道，TLS 仍验证原始官方域名，非 HTTP 代理类型 fail-closed。影策图片若 provider 返回通过官方 allowlist、HTTPS、标准端口且无 userinfo 的媒体 URL，则直接按 OpenAI Images `{url: ...}` 返回，不做服务端下载；data URI 与安全本地缓存仍返回 `b64_json`。
- `/v1/models` 六能力目录与 `/v1/chat/completions` 旧入口都有本轮自动回归覆盖，未建立第二套模型目录或上游客户端。

本轮真实 TDD 证据包括：初始五路由均 404 的鉴权 RED、Images 501 RED、multipart/mask 501 RED、Videos 501 RED、幂等/错误白名单 RED、TTL/容量 RED、媒体缓存与 data URL RED、failed content RED、resolution 映射 RED、畸形 provider status 与 FileCache 异常 RED，以及 FileCache 不支持静默来源 URL 的 `TypeError` RED。最终聚焦：`21 passed, 5 subtests passed in 1.30s`；全量：`334 passed, 99 subtests passed in 101.26s`。`python -m compileall -q src tests scripts` 通过；本轮没有修改页面 JavaScript，因此 JS parse 门禁不适用。最终 `git diff --check` 结果见本任务 QC 报告。

## 2026-08-14 安全返修与 Windows 自启 UI P1

- multipart 总限额继续只作用于 `/v1/images/edits` 与 `/v1/videos`：声明长度超限可预拒绝；声明长度较小或缺失时仍按实际 ASGI receive 累计。新增合同覆盖 forged-low `Content-Length`、chunked、完整 body replay、`http.disconnect` 保真、未完整即 disconnect 不进入下游、超限单次 413，以及下游不读 replay 时正常结束。
- 视频任务 TTL same-update RED 证明旧实现可把刚形成的 `task_timeout` 覆盖为 completed；最小修复采用终态不可逆，terminal task 的 late update 只返回现状。
- 远端媒体代理安全 RED 证明 `CurlOpt.RESOLVE`/curl `CONNECT_TO` 不能作为 HTTP proxy 的可证明 DNS pin。当前显式 HTTP 媒体代理改用固定 IP CONNECT：每一跳先做 allowlist、HTTPS/标准端口、无 userinfo 与 global DNS 校验，CONNECT 目标使用预检公网 IP，随后 TLS `server_hostname` 与请求 `Host` 仍使用原始官方域名；redirect 每跳重新解析，响应按实际字节限额流式读取。非 HTTP 代理类型 fail-closed。无代理下载继续使用 RESOLVE、`verify=True`、关闭自动 redirect 并逐跳重验。影策图片另采用无网络的 allowlisted HTTPS URL 透传。
- Codex 提供的真实管理页证据发现固定自启模板已登记并启用，但同一当前用户可能分别显示为 qualified 与 bare，旧纯字符串相等导致 UI 误报 disabled。本轮 RED 使用纯 synthetic identity；修复只接受 exact，或“当前用户 qualified、观测值 bare 且 leaf 相同”的单向情况。不同 bare leaf 与不同 qualified 前缀即使 leaf 相同仍拒绝。
- 本轮没有读取或输出真实用户名、系统任务 XML、凭据、账号标识、原始提示词/媒体或响应正文；没有操作真实系统自启任务，没有执行真实 Flow，没有 commit/push/部署。
- Fresh 安全+自启聚合：`62 passed, 22 subtests passed in 1.74s`。Fresh full pytest：`364 passed, 107 subtests passed in 100.71s`。最终四核心分两批等价复跑合计 `25 passed, 6 subtests passed`；`compileall -q src tests` 通过；严格 secret-format 扫描计数为 0；`git diff --check` 通过且只有既有 LF/CRLF warning。
