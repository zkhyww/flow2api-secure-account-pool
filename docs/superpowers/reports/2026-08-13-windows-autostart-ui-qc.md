# Windows Autostart UI QC — 2026-08-13

## Implemented contract

- 管理页增加“Windows 登录后自动启动 Flow2API”开关，真实状态来自固定任务 `Flow2API-Local-Account-Pool`，未写入账号数据库。
- 管理 API 为 `GET/POST /api/admin/windows-autostart`，沿用现有 admin-session 鉴权。
- POST 请求只允许布尔 `enabled`；额外 `command`、`path`、`task_name`、`powershell` 字段均被拒绝，不能到达系统 manager。
- 非 Windows 返回 `unsupported`；Windows 状态仅为 `enabled/disabled/error/unsupported`。
- Windows 系统边界只使用服务端固定任务名、当前仓库 venv Python、`main.py`、当前仓库 cwd、当前用户登录触发器、StartWhenAvailable 与失败重启 3 次。
- enable/disable 均先读真实状态，重复调用幂等；系统写失败后重新读取并返回可确认状态与简短原因。
- `start-flow2api.pyw` 双击无控制台；只使用固定 localhost `/health` 与 `/manage`。已就绪只打开管理页；未就绪时固定 argv/cwd、`shell=False` 启动一次并最多等待 60 秒，失败用 GUI 短消息提示。

## RED → GREEN

首轮 B focused RED：`9 failed`，覆盖缺少 Windows autostart service、管理端点、UI 控件/状态逻辑与启动器。

service/API 首轮 GREEN：`7 passed, 2 deselected, 9 subtests passed`。

旧 `.cmd` 路线被 DevSpace 安全层阻止后，独立 fresh 聚焦收敛为仅启动器 1 条失败。按主控指令改为 `.pyw` 后，先得到缺失 `.pyw` 的 RED：`3 failed, 8 passed, 9 subtests passed`；最小实现后 GREEN：`11 passed, 9 subtests passed`。

随后收紧“启动器不接受路径”边界，先得到固定 `REPO_ROOT` 缺失 RED：`2 failed, 9 passed, 9 subtests passed`；最小实现后最终 focused GREEN：`11 passed, 9 subtests passed`。

相关现有管理静态合同：`5 passed, 11 subtests passed`。

最终 full pytest：`313 passed, 94 subtests passed`。

## Final gates

- `compileall`: PASS。
- `static/manage.html` inline JavaScript：`2/2` parse PASS。
- credential-pattern candidates：`0`，仅记录计数。
- `git diff --check`: PASS；只有既有 LF/CRLF 提示。

## Files

本批新增/修改：

- `src/services/windows_autostart.py`
- `src/api/admin.py`
- `static/manage.html`
- `tests/test_windows_autostart_contract.py`
- `start-flow2api.pyw`
- `docs/superpowers/specs/2026-08-13-windows-autostart-ui-design.md`
- `docs/superpowers/plans/2026-08-13-windows-autostart-ui.md`
- `docs/superpowers/reports/2026-08-13-windows-autostart-ui-qc.md`

`start-flow2api.cmd` 是旧方案留下的未完成新增半成品，不属于最终交付。当前 DevSpace 连接器没有删除文件操作，Codex 在干净副本收口时必须删除该文件；不要把它纳入发布或验收。

## Execution boundary

本轮只执行 mock 系统边界与静态/单元测试；未调用真实生成，未对真实计划任务执行切换，未启动或重启服务，未启停账号，也未执行版本库或部署写操作。
