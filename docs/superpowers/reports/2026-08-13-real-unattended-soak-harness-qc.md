# Real Unattended Soak Harness QC — 2026-08-13

## Scope

本报告只记录 `scripts/real_unattended_soak.py` 与 `tests/test_real_unattended_soak_contract.py` 的代码侧无人值守稳定性测试器闭环。未执行真实 Flow 生成，未重启服务，未启停账号，未读取数据库、Cookie、Token、Profile、账号邮箱、完整媒体 URL、响应正文或其他凭据。

## Review result

- 本地调用固定 `127.0.0.1:8000/v1/chat/completions`。
- API key 仅从环境变量 `FLOW2API_SOAK_API_KEY` 注入；CLI 不提供 key 参数，报告不包含 key。
- CLI 支持 `--duration-hours`、`--interval-seconds`、`--video-every`、`--idle-wait-seconds`、`--output`。
- 默认低频、每轮单请求、无盲目重试。
- 图片使用当前 Nano Banana 2 landscape 默认 1K 路径；视频使用 `omni_10s`。
- 报告字段保持严格聚合 allowlist；错误类别仅限既定 9 类。
- 报告采用临时文件加 `os.replace` 原子覆写。
- 最终 idle 后重新读取服务后代进程，只聚合 browser process count 与 RSS；账号资源无法无凭据读取时保持 `unknown`。

## RED → GREEN

Fresh focused RED：`1 failed, 4 passed, 7 subtests passed`。失败来自测试固定目录残留，并非产品写入逻辑。

最小修复：测试改用 `tempfile.TemporaryDirectory()`；旧残留目录保持原样。

Fresh focused GREEN：`5 passed, 7 subtests passed`。

最终 full pytest：`313 passed, 94 subtests passed`。

## Final gates

- `compileall`: PASS。
- `static/manage.html` inline JavaScript：`2/2` parse PASS。
- credential-pattern candidates：`0`，仅记录计数。
- `git diff --check`: PASS；只有既有 LF/CRLF 提示。

## Files and non-actions

本项直接修改 `tests/test_real_unattended_soak_contract.py`；`scripts/real_unattended_soak.py` 已审查且本轮未改产品逻辑。

未执行真实生成、服务重启、账号启停、commit、push、PR、issue 或部署。
