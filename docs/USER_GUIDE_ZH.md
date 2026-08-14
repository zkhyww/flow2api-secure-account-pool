# Flow2API 本地增强版中文使用说明

> 面向 Windows 本地使用。本文不记录本机账号、额度、密钥、提示词、媒体地址或任务内容。

## 1. Windows 首次安装

建议使用 Python 3.11；上游 README 标注 Python 3.8+。在项目目录执行：

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

上面只安装 Flow2API 的运行依赖。开发、CI 或交付前要执行全量测试时，再额外安装独立测试依赖：

```powershell
venv\Scripts\python.exe -m pip install -r requirements-test.txt
```

`requirements-test.txt` 不属于生产运行依赖；只运行服务时无需安装它。不要把 API Key、Google Cookie 或其他凭据写进启动命令。

## 2. 一键启动与开机自启

双击项目根目录的 `start-flow2api.pyw` 即可本地启动。服务已经健康运行时，它不会重复拉第二份服务，只会打开管理页；服务未运行时，它会使用当前项目自己的 venv 启动并等待健康检查。

管理页提供“Windows 登录后自动启动 Flow2API”开关。它读取 Windows Task Scheduler 的真实状态，不在数据库里另存一个假开关。修改后建议刷新页面确认状态。

## 3. 第一次登录后先改密码

上游 README 的默认管理账号是 `admin` / `admin`。这只是上游默认说明，不代表你当前机器仍在使用这个密码。首次登录后应立即修改管理员密码。

## 4. 添加 Google 账号

从管理页发起“添加账号”：

1. Flow2API 打开独立的本地浏览器登录流程。
2. 正常完成一次 Google 登录。
3. 如果 Google 或 Flow 要求验证码、人机验证或再次确认登录，需要人工完成；本项目不会绕过风控。
4. 登录成功后，账号进入现有 Token/账号持久化体系。正常服务重启后可从持久化状态恢复，不要求安装浏览器插件。

运行中的浏览器 profile 是临时运行资源，不应当当作账号备份源。上游登录态以后如果失效，仍可能需要再次人工登录或验证。

## 5. 内置测试页

打开 `http://127.0.0.1:8000/test`。

图片页可选公开模型、比例和 1K/2K/4K。1K/2K 是当前常用档；4K 可能需要更高会员权限，不满足时会返回类似 `membership_required` 的公开错误，不会偷偷降级。

视频页中，Omni Flash 公开 8 秒和 10 秒并默认 10 秒；Veo 3.1 Lite/Fast/Quality 的公开能力仍以 8 秒为主。只有模型明确支持参考图时才应上传参考图，不支持时会 fail-closed。

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
- 浏览器按需启动，任务结束后由 idle 回收。
- 429 会触发降并发和冷却；认证、membership、captcha、上游错误分别归类，不会全部误判成 429。
- 可能已被上游接受的任务不会盲目重复提交。

## 10. 备份和恢复

默认主数据库是 `data/flow.db`。

建议先正常停止 Flow2API，再备份 `data/flow.db`。如果你另外维护了明确需要保留的本地配置文件，也一并备份；不要把 `tmp` 媒体缓存或浏览器临时 profile 当成账号状态备份。

恢复后启动服务并在管理页核对账号状态。如果上游登录态已经失效，仍需按正常 Google 流程重新登录或验证。

## 11. 常见故障

- **服务红灯/页面打不开**：先看一键启动是否报告失败，再检查本地 `/health`，不要重复启动很多实例。
- **authentication / 账号失效**：回管理页正常修复登录，不要无限重试生成。
- **captcha**：按页面要求人工完成验证码，不绕过风控。
- **membership**：所选模型或清晰度超出账号权限时改用已有权限的公开能力。
- **rate limit / 429**：等待冷却并降低并发，不要连续猛重试。
- **影策视频媒体失败**：检查媒体代理类型和网络是否允许安全隧道；安全校验失败时不会做不安全降级。

## 12. 本次交付范围

本次以 Windows 本地优先为目标。Zeabur、Linux 云端登录入口和生产部署不在本次交付范围内，也不要把本地验证结果直接等同于云部署验收。
