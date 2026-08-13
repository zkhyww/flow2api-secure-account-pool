# Windows Autostart UI Design

## Goal

在现有 Flow2API 管理配置页增加“Windows 登录后自动启动 Flow2API”开关。开关的事实来源必须是 Windows Task Scheduler 中固定任务 `Flow2API-Local-Account-Pool` 的当前真实状态，而不是数据库字段。仓库根目录同时提供双击可用的一键启动 `.pyw`，便于没有计划任务时手工启动或打开管理页。

## Scope and boundaries

- 只管理固定任务名 `Flow2API-Local-Account-Pool`。
- 只允许固定启动模板：当前仓库 `venv\Scripts\python.exe` 执行 `main.py`，工作目录为当前仓库根目录。
- 客户端请求只包含布尔值 `enabled`；不得提交 task name、命令、路径、PowerShell 或其他可执行内容。
- 不新增数据库配置，不读取或保存账号凭据，不改变服务配置。
- 非 Windows 平台返回 `unsupported`，不调用计划任务命令。
- 本轮自动化测试只 mock 系统命令边界，不创建、修改或删除真实计划任务。
- `.pyw` 不读取、写入或显示 API key、账号或其他凭据。

## Components

### `src/services/windows_autostart.py`

封装唯一 Windows Task Scheduler 边界。模块从自身文件位置推导仓库根目录，因此任务动作参数不可由 HTTP 客户端控制。

公开状态只有：

- `enabled`：固定任务存在、启用，并且动作、工作目录、当前用户登录触发器、`StartWhenAvailable`、失败重启 3 次都符合固定模板。
- `disabled`：任务不存在、被禁用，或固定任务存在但模板不匹配；再次 enable 会修复该固定任务。
- `error`：Windows 状态读取失败或返回无法解析的数据。
- `unsupported`：非 Windows。

enable 流程先读取真实状态。若已经 `enabled` 则不调用注册命令；否则使用固定 PowerShell 模板创建或 `-Force` 修复当前用户登录任务。disable 流程若任务不存在则幂等返回 `disabled`；若固定任务存在则只注销该任务。系统写操作失败后重新读取真实状态；UI 不做乐观翻转，并显示失败原因。

状态读取和写入命令的 stdout/stderr 不直接返回给 API。错误文本只暴露稳定的操作阶段与退出码/异常类型，避免把系统环境细节带到页面。

### `src/api/admin.py`

新增两个管理端点：

- `GET /api/admin/windows-autostart`
- `POST /api/admin/windows-autostart`，请求体仅 `{ "enabled": boolean }`

两个端点都使用现有 `Depends(verify_admin_token)`。公共 API 路由不增加对应入口。

### `static/manage.html`

系统配置区增加开关、状态文字和原因文字。页面加载时调用 GET 获取真实状态：`enabled` 才勾选；`disabled` 不勾选；`unsupported` 禁用开关；`error` 保持不可误导的未勾选状态并显示原因。

用户切换时只 POST 布尔值。请求完成后以服务端返回状态重新渲染；失败不保留用户的乐观勾选结果。

### `start-flow2api.pyw`

根目录双击启动器，无控制台窗口：

1. 固定探测 `http://127.0.0.1:8000/health`。
2. 已就绪：直接打开 `http://127.0.0.1:8000/manage`，不启动第二个进程。
3. 未就绪：验证当前仓库 `venv\Scripts\python.exe` 与 `main.py` 存在。
4. 仅以固定 argv、当前仓库 cwd、`shell=False` 启动一次 `main.py`。
5. 最多等待 60 秒；就绪后打开管理页。
6. 文件缺失、启动失败或健康检查超时时用 `tkinter.messagebox` 显示简短错误。
7. 不解析命令行参数，也不接受外部命令、路径或 URL。

此前被 DevSpace 安全层阻止后留下的 `start-flow2api.cmd` 是未完成半成品，不属于最终交付；当前连接器没有删除文件接口，由主控在干净副本收口时删除该新增文件。

## Scheduled-task template

固定任务使用当前 Windows 用户：

- Task name: `Flow2API-Local-Account-Pool`
- Execute: `<repo>\venv\Scripts\python.exe`
- Arguments: `main.py`
- Working directory: `<repo>`
- Trigger: current-user logon
- Run level: limited/current user interactive token
- `StartWhenAvailable = true`
- `RestartCount = 3`
- `RestartInterval = 1 minute`

状态检查按 Windows 路径规则做大小写不敏感的归一化比较，不接受客户端覆盖上述值。

## Error handling

- 非 Windows：`unsupported`，原因说明仅 Windows 支持。
- 查询命令失败或 JSON 不可解析：`error`。
- enable/disable 命令失败：重新读取状态；若可读则保持真实 `enabled`/`disabled` 状态并附操作失败原因，否则返回 `error`。
- UI 始终以后端返回状态为准，不在请求前永久改变显示状态。

## Tests

TDD RED 先覆盖：固定任务模板与客户端不可注入、admin-session 鉴权、Windows/非 Windows 状态、enable/disable、重复调用幂等、失败后状态保持、管理页文案与真实状态渲染、`.pyw` 已就绪去重、固定 argv/cwd、等待成功与失败提示。GREEN 后再跑相关管理/静态合同、全量 pytest、compileall、整页 JS parse、credential-pattern 计数扫描与 `git diff --check`。
