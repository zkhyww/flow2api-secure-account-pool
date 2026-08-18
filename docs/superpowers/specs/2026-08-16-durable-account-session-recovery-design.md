# Flow2API 可持久登录与过期恢复设计

## 背景与现场事实

2026-08-16 的真实故障不是账号记录丢失，而是认证恢复链路断开：数据库仍有 5 个账号、加密 Google Cookie、ST、项目关联和自动刷新配置，但 5 个 AT 全部过期。协议刷新失败后，`TokenManager` 把账号设为 `is_active = 0`；后台刷新器又只读取 `get_active_tokens()`，因此这些账号永久退出恢复队列。3 个账号被 Google 拒绝现有 Cookie，2 个账号没有完成 OAuth callback。

现有 personal 浏览器运行时使用临时/无痕 profile。它能在 Cookie 尚有效时跨服务重启恢复，但临时 profile 会在 idle 后清理，不能在 Cookie 被 Google 作废时充当第二登录来源。这与用户已确认的目标“登录一次、关机后仍可恢复、浏览器按需启动并自动关闭”不完全一致。

## 已确认目标

1. 每个 Google 账号保留独立、可复用且只属于本机 Windows 用户的登录 Profile。
2. 服务启动时不批量打开浏览器；AT/ST 即将过期或任务需要时才按需打开对应账号的恢复浏览器，完成后关闭。
3. 日常图片/视频生成继续复用现有账号池、dense-pack、并发学习、最多 10 个浏览器保护、熔断、配额和 idle 回收，不新增第二套调度器。
4. 一次刷新失败不能永久禁用账号。临时失败进入退避队列；只有 Google 明确要求人工登录时标记“需要重新登录”。
5. 用户重新登录时优先复用该账号原 Profile 的已有登录态。为避免交互登录把错误身份直接写进旧 Profile，实际执行时先把现有 Profile 安全复制到隔离候选目录，在候选中登录；身份匹配后把候选凭据、Profile 引用、认证成功状态和显式启用状态作为同一个数据库事务提交，再清理旧目录。事务失败则回滚全部账号行变化，保留旧引用/旧目录并清理候选。若候选或旧目录因临时文件锁不能立即删除，只在 Profile 根内留下含不透明 key 的 cleanup marker，并在下一次候选创建时重试；不得把未跟踪的孤儿目录当成完成状态。旧账号确实没有 Profile 时才从空候选开始。成功后恢复调度；登录账号与目标账号不一致时拒绝覆盖。
6. 不依赖扩展、配对码、账号密码或长期打开的普通 Chrome。

## 方案比较

### A. 只加强加密 Cookie 重试

改动最小，但 Google 撤销 Cookie 后仍无恢复来源，只能重新导入。它不能满足“只登录一次”的目标，因此不采用。

### B. 每账号持久 Profile + 认证恢复状态机（采用）

首次登录使用专用 Profile；登录完成后关闭浏览器但保留 Profile。正常生成仍使用现有临时 worker 和加密 Cookie；只有 Cookie 协议刷新失败时，才按需用持久 Profile 恢复 ST/Cookie，随后立刻关闭。这样不改变现有并发结构，也不会因为 200 个账号而打开 200 个浏览器。

### C. 所有账号共享一个 Chrome Profile

磁盘占用较少，但 Google 多账号切换容易串号，无法稳定绑定 reCAPTCHA、Cookie 和项目，且并发时不能安全隔离，因此不采用。

## 数据模型

继续以现有 `tokens` 表为唯一账号事实源，只增加状态和引用字段：

- `account_profile_key`：随机生成的非身份标识，只引用 `data/account_profiles/<key>`；API 和日志不返回路径。
- `auth_state`：`ok`、`refresh_pending`、`backoff`、`reauth_required`。
- `auth_failure_count`：连续认证恢复失败次数。
- `auth_next_retry_at`：下一次允许后台恢复的时间。
- `last_auth_error_class`：只保存白名单错误类，不保存原始异常、URL 或响应正文。

`is_active` 只表示用户是否允许账号参与调度。认证失败不得再调用 `disable_token()`。迁移不擅自启用历史停用账号；用户通过“重新登录并启用”明确恢复当前已停用账号。

## Profile 存储与安全

- Profile 根目录固定为运行数据目录下的 `account_profiles`，每账号使用随机 key，不使用邮箱、token、项目名或提示词作为目录名。
- 路径解析必须确认最终路径仍在该根目录内，拒绝绝对路径、`..`、链接逃逸和外部目录。
- Profile 只用于 Flow/Google 登录，不用于普通浏览；不记录浏览历史、提示词或媒体。
- Windows Chrome Cookie 继续受当前 Windows 用户 DPAPI 保护；目录和数据库都不得进入 Git、ZIP、日志或公开报告。
- Profile 是本机当前 Windows 用户绑定的状态。复制到另一台设备不保证可解密，另一台设备需要各自登录一次。
- 备份必须在服务正常停止后进行；同机恢复同时备份 `flow.db` 与 `account_profiles`。临时 worker profile、媒体缓存和日志仍不得备份为账号状态。

## 首次登录与重新登录

1. “添加 Google 账号”预分配一个待确认的随机 Profile，在该 Profile 中打开有头浏览器。
2. 用户只处理 Google 登录、Passkey、验证码或两步验证；系统不索取密码和验证码。
3. 捕获到 Flow Session 后先关闭浏览器，再通过现有 `TokenManager` 新增或更新账号，并把 Profile key 写入同一 token。
4. 已存在账号更新时，必须核对登录结果身份；匹配后原子替换 Profile 引用，失败则保留旧 Profile 和旧账号记录。
5. “重新登录”复用目标账号现有 Profile 的登录态，但不直接原地修改：现有 Profile 先复制为隔离候选，浏览器只打开候选；成功捕获后先用 ST→AT 返回身份核对目标账号，匹配才在单个 SQLite 事务中提交加密 Cookie、ST、AT、Profile 引用、认证成功状态与显式启用状态。事务成功后清理旧目录；事务失败则旧引用保持不变并清理候选。只有旧账号没有 Profile 时才创建空候选。
6. 公共轮询只返回 `stage/status/error_class`，不返回账号、Cookie、Profile、Token、URL 或响应正文。

## 自动恢复数据流

1. 后台刷新器读取 `is_active = 1 AND auto_refresh_enabled = 1` 的恢复候选，不再以 AT 是否有效决定能否进入恢复队列。
2. AT 临近过期时先走现有 ST→AT；失败后走现有加密 Cookie 协议刷新。
3. 协议刷新失败且账号有 Profile 时，获取账号级恢复锁，按需启动该 Profile 的无头浏览器，恢复 Session/Cookie，写回现有加密字段，然后关闭浏览器。
4. 成功时设置 `auth_state = ok`、失败计数归零并清除下一次重试时间。
5. 网络、临时 OAuth 或浏览器启动失败进入指数退避，避免请求风暴；退避期间调度器跳过该账号但后台到期后继续恢复。
6. Google 明确拒绝登录、要求交互验证或 Profile 不存在时设置 `reauth_required`。该账号不参与生成，但不改变用户的启用意图。
7. 所有账号不可用时，API 返回稳定的 `reauth_required` 或 `auth_backoff`，管理页显示需要处理的账号数量，不再显示“没有保存账号”。

## 并发与生命周期

- 同一账号的认证恢复使用单飞锁；并发请求只触发一次真实刷新或一次 Profile 唤醒。
- 所有 personal 浏览器实例（普通池 worker、onboarding、重新登录、认证恢复）共享同一个最多 10 个存活浏览器进程 lease。实例在真正启动前取得 lease，并一直持有到 shutdown `finally` 完成后释放；原有启动并行度闸门继续只负责抑制 Windows 启动尖峰，两者职责不混用，也不新增账号调度器。
- 日常生成仍优先在单账号容量内 dense-pack；Profile 数量不等于运行浏览器数量。
- 服务启动时浏览器进程为 0；恢复成功、失败或超时都必须关闭恢复浏览器。
- idle reaper 和现有临时 worker profile 清理保持不变；`account_profiles` 明确排除在临时清理范围外。

## 迁移与回滚

- 新字段通过现有 SQLite 增量迁移加入，旧账号默认 `auth_state = ok`，但不自动改变 `is_active`。
- 现有账号没有持久 Profile。当前 Cookie 已被 Google 拒绝，因此需要在新 UI 中逐个“重新登录并启用”一次；之后同机重启不再依赖临时 profile。
- 切换运行服务前，先复制当前 `flow.db` 到带时间戳的本机备份位置并记录哈希，不读取其中凭据。
- 回滚时停止服务，恢复数据库备份并切回旧源码。新增 Profile 目录保留但旧版不会读取，不做自动删除。

## 测试与验收

### 自动 RED/GREEN

1. AT/ST/Cookie 刷新全部失败后账号仍保持用户启用，进入 `backoff` 或 `reauth_required`，不调用 `disable_token()`。
2. 后台恢复扫描包含 AT 已过期但用户启用的账号；手工停用账号不进入扫描。
3. 同一账号并发刷新只执行一次；退避未到期不重复请求，到期后可恢复。
4. Profile key 生成、路径包含检查、跨账号隔离、重启后复用和临时清理排除均有合同测试。
5. onboarding 浏览器关闭后 Profile 仍存在；模拟重启后从同一 Profile 恢复；恢复浏览器最终归零。
6. 身份不匹配、链接逃逸、损坏 Profile、缺失 Profile 和交互验证全部 fail-closed。
7. UI/API 只显示稳定状态和数量，不输出凭据或 Profile 路径。
8. 0/1/200/500 账号合同、3/5/10 worker、dense-pack、影策适配、图片/视频模型目录和 idle 生命周期保持通过；另外用确定性 10/11 并发合同证明第 11 个浏览器在进入真实启动边界前等待，并覆盖启动失败、启动超时、取消、close 异常以及普通 personal pool 与 reauth/recovery 混合占用时的 lease 释放/共享。

### 本机真实验收

1. 用户只需对现有账号逐个重新登录一次；先用一个账号完成端到端验收，再迁移其余账号。
2. 登录后关闭浏览器和服务，重新启动，确认账号无需重新导入。
3. 等待 AT 过期窗口或使用不接触真实凭据的受控过期夹具，确认自动启动恢复浏览器、刷新成功并关闭。
4. 执行一次低成本图片和一次 Omni Flash 视频；失败不自动重复付费提交。
5. 等待真实 idle TTL，确认项目浏览器进程归零；再提交任务确认自动唤醒。
6. 全量 pytest、compileall、`git diff --check`、运行态/凭据扫描和干净交付扫描全部通过后，才更新 ZIP、SHA 和私有仓库。

## 非目标

- 不绕过 Google 验证、reCAPTCHA、会员或风控。
- 不保存账号密码、Passkey、验证码或恢复码。
- 不修改 Framefield Studio、gflow、插件或影策安全底座。
- 不增加云端/Zeabur 登录方案，不承诺跨设备复制 Profile 后仍可登录。
- 不重写账号池、并发调度、任务队列、日志体系或媒体解析。
