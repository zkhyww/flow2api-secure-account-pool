# Flow2API 本地增强版中文使用说明

> 面向 Windows 本地使用。本文不记录本机账号、额度、密钥、提示词、媒体地址或任务内容。

## 1. Windows 首次安装

准备项：

- 安装 Python 3.11 或更高版本（64 位），安装时勾选 `Add Python to PATH`。
- 安装最新版 Google Chrome，供本机账号登录和认证恢复使用。
- 从 `https://github.com/zkhyww/flow2api-secure-account-pool` 下载 ZIP 后完整解压，或使用 Git clone。不要只复制几个脚本。

最简单的启动方式是双击根目录的 `start-flow2api.cmd`。第一次运行会自动：

1. 创建项目自己的 `venv`。
2. 安装 `requirements.txt` 中的运行依赖；依赖未变化时以后不会重复安装。
3. 启动服务。首次启动时程序会自动创建 `data/flow.db` 和所需表结构，不需要单独安装数据库。

如果一键初始化失败，先确认 Python 3.11 或更高版本可在命令行运行、网络能访问 Python 包源，再重新双击。需要手工安装时才执行：

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

上面只安装 Flow2API 的运行依赖。开发、CI 或交付前要执行全量测试时，再额外安装独立测试依赖：

```powershell
venv\Scripts\python.exe -m pip install -r requirements-test.txt
```

`requirements-test.txt` 不属于生产运行依赖；只运行服务时无需安装它。不要把 API Key、Google Cookie 或其他凭据写进启动命令。

仓库中没有、也不应该有另一台电脑的数据库或登录数据。`data/`、`config/setting.toml`、Google Cookie、账号 Profile 和 API Key 均被 Git 排除：下载代码后页面里没有原机器的账号是正常且必要的安全设计。每台电脑首次使用时都要在本机逐个添加账号。

## 2. 一键启动与开机自启

优先双击项目根目录的 `start-flow2api.cmd`。它会选择已有 venv；新下载的目录没有 venv 时，会调用本机默认的 Python 3.11 或更高版本执行 `start-flow2api.pyw` 并自动补齐环境。服务已经健康运行时，它不会重复拉第二份服务，只会打开管理页；服务未运行时，它会启动并等待健康检查。

管理页提供“Windows 登录后自动启动 Flow2API”开关。它读取 Windows Task Scheduler 的真实状态，不在数据库里另存一个假开关。修改后建议刷新页面确认状态。

## 3. 第一次登录后先改密码

上游 README 的默认管理账号是 `admin` / `admin`。这只是上游默认说明，不代表你当前机器仍在使用这个密码。首次登录后应立即修改管理员密码。

## 4. 添加 Google 账号

从管理页发起“添加账号”：

1. Flow2API 打开独立的本地浏览器登录流程。
2. 正常完成一次 Google 登录。
3. 如果 Google 或 Flow 要求验证码、人机验证或再次确认登录，需要人工完成；本项目不会绕过风控。
4. 登录成功后，该账号会获得独立的本机持久登录 Profile，并继续使用现有 Token/账号池；不要求安装浏览器插件。
5. 从本次升级前已经存在的旧账号没有持久登录 Profile，需要在管理页逐个执行一次“重新登录并启用”。完成这一次迁移后，同一台机器、同一 Windows 用户下的服务重启可以继续从该账号的持久登录状态恢复。
6. 换到另一台机器时不要复制或导入 Profile；每台机器都要分别为每个账号正常登录一次。验证码、Passkey、两步验证或其他 Google/Flow 风控仍由用户在打开的浏览器里正常完成，本项目不会绕过。

账号持久 Profile 只用于认证恢复，位于本地 `data/account_profiles/<不透明随机键>/`，不会作为 API 字段、日志字段或导出内容。普通图片/视频生成 worker 仍使用临时 Profile，不会改成长期保存。显式“重新登录并启用”会优先复用目标账号已有的本机登录态：系统先把现有 Profile 安全复制到隔离候选中，只在候选里打开交互浏览器；身份核对一致后，候选凭据、Profile 引用、认证成功状态和显式启用状态会在同一个数据库事务中提交，事务成功后才清理旧目录；事务失败或串号则保留旧 Profile 引用并清理候选。若 Windows 临时文件锁阻止立即删除，系统只在 Profile 根内登记不透明 cleanup marker，并在下一次候选创建时重试，不把目录路径或内容暴露到 API/日志。只有旧账号确实没有 Profile 时才从空候选开始。浏览器只在添加账号、显式重新登录或认证自动恢复时按需启动；所有 ordinary personal worker 与这些认证浏览器共享同一个最多 10 个存活浏览器进程 lease，lease 从启动前一直持有到关闭清理完成，完成、失败、超时或取消都必须释放。

单次 Cookie/ST/AT 刷新失败只会进入“等待自动恢复”或“稍后重试”等认证恢复状态，不会把账号永久停用。`is_active` 只由用户自己的启用/停用操作决定。需要人工重新登录时，管理页会显示“需要重新登录”。

## 5. 内置测试页

打开 `http://127.0.0.1:8000/test`。

图片页可选公开模型、比例和 1K/2K/4K。1K/2K 是当前常用档；4K 可能需要更高会员权限，不满足时会返回类似 `membership_required` 的公开错误，不会偷偷降级。

视频页中，旧 Omni Flash 与新增 Omni 1.1 Flash 都提供独立入口：文生公开 8 秒和 10 秒并默认 10 秒，参考图入口当前只公开已验证的 8 秒；旧 Omni 的 ID 和行为没有被替换。Veo 3.1 Lite/Fast/Quality 的公开能力仍以 8 秒为主。只有模型明确支持参考图时才应上传参考图，不支持时会 fail-closed。Google 官方完整能力表可查看：`https://support.google.com/flow/answer/16352836`；官方网页存在但本兼容 API 尚未真实验收的组合不会提前出现在菜单里。

## 6. 查看和更换 API Key

在管理页“系统配置 / API 密钥配置”查看或更换 Flow2API API Key。

推荐请求头：

```text
Authorization: Bearer <your_api_key>
```

Gemini 兼容调用也可使用：

```text
x-goog-api-key: <your_api_key>
```

不要把 API Key 放进 URL、书签、截图、日志或共享文档。

## 7. 影策（Framefield Studio）接入

自定义渠道填写：

- Base URL：`http://127.0.0.1:8000`
- API Key：使用 Flow2API 管理页中的 Key；文档示例统一写 `<your_api_key>`
- 图片协议：`openai-image`
- 视频协议：`newapi`

图片端点：`POST /v1/images/generations`；参考图编辑：`POST /v1/images/edits`。

图片可能返回 `b64_json`，也可能返回官方媒体 `url`。只有官方 allowlist 内、HTTPS、标准端口、无 userinfo 的 URL 才允许直接返回；其他远端 URL 不会被原样反射。

视频端点：

- `POST /v1/videos` 创建任务
- `GET /v1/videos/{id}` 轮询
- `GET /v1/videos/{id}/content` 读取完成后的本地二进制

视频不会把完整上游媒体 URL 存进 registry，也不会让 `/content` 重定向到上游地址。远端视频先经过安全下载并落成本地文件名，再由 `/content` 读取。

**当前影策视频 task ID 只存在于本进程内。服务重启后，旧 task ID 不保证还能查询。**

Codex 做受控真实验收时，可使用现有 `scripts/real_unattended_soak.py`。验收凭据只从既有环境变量读取，不写入命令行或报告：

```text
venv\Scripts\python.exe scripts\real_unattended_soak.py --kind image
venv\Scripts\python.exe scripts\real_unattended_soak.py --kind video
```

`--kind image` 只执行一次图片 create，不因传输失败自动重复生成；`--kind video` 使用幂等 create 后继续 poll 和 content。两种 smoke 只报告状态、错误分类、是否得到媒体、耗时和 HTTP 状态；视频额外报告媒体字节数，不输出任务标识、凭据、提示词、完整 URL 或媒体正文。真实调用只由 Codex 在受控验收阶段执行，不属于普通自动门禁。

## 8. 媒体代理安全边界

显式 HTTP 媒体代理会走固定解析的安全隧道：服务先校验官方域名、HTTPS、标准端口和公网 DNS，再把代理连接目标固定到预检的公网 IP；TLS 证书仍按原始官方域名验证。redirect 每一跳都会重新检查，下载也有流式字节上限。

当前安全代理路径支持 `http://` 媒体代理。其他未实现的代理类型会 fail-closed，不会降级成让代理自行重新解析官方域名的不安全模式。

如果代理拒绝安全隧道、TLS 校验失败或网络不可用，视频任务会公开失败；服务不会为了“能下载”而关闭证书验证或放弃 DNS pin。

## 9. 账号池和浏览器原则

- 200 个账号是本轮验收规模，不是代码硬上限。
- 调度优先使用已经活跃且有安全容量的账号，再启用下一个账号。
- 同时存活的浏览器进程最多 10 个；资源不足时排队。
- 普通生成浏览器仍按需启动并由 idle 回收；账号认证恢复浏览器在一次恢复结束、失败或超时后立即关闭。
- 认证状态只公开“正常 / 等待自动恢复 / 稍后重试 / 需要重新登录 / 已停用”；认证失败不会改写用户的启停选择。
- 429 会触发降并发和冷却；认证、membership、captcha、上游错误分别归类，不会全部误判成 429。
- 可能已被上游接受的任务不会盲目重复提交。

## 10. 备份和恢复

默认主数据库是 `data/flow.db`。

如果要保留本机的可持久登录恢复能力，必须先正常停止 Flow2API，再把 `data/flow.db` 与整个 `data/account_profiles/` 作为同一份本机备份成对保存；不要在服务运行时只复制其中一项，也不要单独读取、修改或拼接某个账号的 Profile 内容。如果你另外维护了明确需要保留的本地配置文件，也可在停服后一起备份。`tmp` 媒体缓存和普通生成的临时 Profile 不属于账号恢复备份。

这份成对备份只用于同一台机器、同一 Windows 用户下的本机恢复，不是跨机器迁移格式，也不得放入 Git、交付 ZIP、日志或公开报告。跨机器恢复时应在目标机器逐个重新登录，而不是复制 Profile。上游登录态失效或要求额外验证时，也仍需按正常 Google 流程重新登录或验证。

## 11. 常见故障

- **服务红灯/页面打不开**：先看一键启动是否报告失败，再检查本地 `/health`，不要重复启动很多实例。
- **新电脑双击后没有服务**：确认已安装 Python 3.11 或更高版本（并加入 PATH）、Google Chrome，且网络可安装 `requirements.txt`；完整解压仓库后再运行，不要从压缩包预览窗口直接启动。
- **新电脑登录页能开但没有旧账号**：这是正常现象。Git 不同步数据库、Cookie 或 Profile；请在新电脑逐个添加账号，不能靠复制另一台机器的 `data/` 绕过登录。
- **账号显示正常但生成时 401**：新版本会把账号转为“稍后重试/需要重新登录”并在自动调度时切换下一个可用账号；明确指定账号的诊断请求不会偷偷换号。
- **等待自动恢复 / 稍后重试**：先让后台恢复按退避继续，不要手工高频刷新，也不会因此永久停用账号。
- **需要重新登录**：回管理页使用“重新登录并启用”，在打开的浏览器中完成正常 Google 登录或验证。
- **captcha**：按页面要求人工完成验证码，不绕过风控。
- **membership**：所选模型或清晰度超出账号权限时改用已有权限的公开能力。
- **rate limit / 429**：等待冷却并降低并发，不要连续猛重试。
- **影策视频媒体失败**：检查媒体代理类型和网络是否允许安全隧道；安全校验失败时不会做不安全降级。

## 12. 本次交付范围

本次以 Windows 本地优先为目标。Zeabur、Linux 云端登录入口和生产部署不在本次交付范围内，也不要把本地验证结果直接等同于云部署验收。
