# 本地账号池合成压力与恢复量化验收

日期：2026-08-13

## 结论

本轮新增可重复的离线合成压力夹具，复用现有负载均衡、并发学习、任务恢复、配额预留、分页与个人浏览器池实现。夹具使用临时数据库、假账号与 fake browser runtime；默认不联网，不读取真实数据库、账号或凭据，不启动 Chrome，不执行真实 Flow。

功能门禁与性能采样分离：正确性由 pytest 断言，耗时与内存仅作为本次环境快照记录，不设置脆弱的绝对时间阈值。18 条量化记录均满足 success_count=operation_count、failure_count=0、leak_count=0、duplicate_submit_count=0、error_attribution_accuracy=1.0。

## TDD 证据

RED：

- 命令：`venv/Scripts/python.exe -m pytest -q tests/test_batch7_synthetic_stress.py`
- 结果：`3 failed in 0.11s`
- 失败原因：缺少目标离线压力夹具，以及缺少严格白名单的持久化量化报告。

GREEN：

- Batch 7 聚焦：`3 passed in 39.76s`
- 既有调度、恢复、配额、浏览器生命周期、分页与 Batch 7 联合聚焦：`50 passed, 6 subtests passed in 44.64s`
- 全量：`297 passed, 78 subtests passed in 81.11s`
- `venv/Scripts/python.exe -m compileall -q src tests scripts`：通过。
- `node --check static/test-page-capabilities.js`：通过。
- 本轮新增压力资产 credential-pattern scan：0 命中；只报告计数。
- `git diff --check`：通过；仅有既有 LF/CRLF 转换提示。

采样命令：

`venv/Scripts/python.exe -m scripts.synthetic_account_pool_stress --samples 5`

机器可读结果位于 `docs/validation/2026-08-13-synthetic-account-pool-stress-validation.json`。每条记录严格只包含批准的 16 个聚合字段。

## 量化结果

| scenario | account_count | worker_limit | operation_count | queued_count | p50_ms | p95_ms | p99_ms | throughput_ops_s | peak_memory_bytes | max_live_workers |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| account_pagination_db | 0 | 0 | 5 | 0 | 2.733 | 3.206 | 3.206 | 155.645 | 152659 | 0 |
| account_pagination_api | 0 | 0 | 5 | 0 | 2.700 | 3.450 | 3.450 | 334.410 | 68384 | 0 |
| dense_pack | 0 | 0 | 12 | 0 | 0.051 | 0.070 | 0.070 | 17182.131 | 25120 | 0 |
| account_pagination_db | 1 | 0 | 5 | 0 | 2.317 | 3.022 | 3.022 | 361.219 | 66515 | 0 |
| account_pagination_api | 1 | 0 | 5 | 0 | 2.609 | 3.313 | 3.313 | 333.923 | 70343 | 0 |
| dense_pack | 1 | 0 | 12 | 9 | 0.128 | 0.270 | 0.270 | 5968.071 | 51949 | 0 |
| account_pagination_db | 200 | 0 | 40 | 0 | 2.809 | 3.401 | 3.432 | 342.462 | 97514 | 0 |
| account_pagination_api | 200 | 0 | 40 | 0 | 4.287 | 5.258 | 5.515 | 231.867 | 137921 | 0 |
| dense_pack | 200 | 0 | 30 | 0 | 13.483 | 14.219 | 16.048 | 73.889 | 4998290 | 0 |
| account_pagination_db | 500 | 0 | 100 | 0 | 3.065 | 3.972 | 4.172 | 319.709 | 99611 | 0 |
| account_pagination_api | 500 | 0 | 100 | 0 | 4.201 | 5.535 | 6.411 | 226.967 | 178409 | 0 |
| dense_pack | 500 | 0 | 30 | 0 | 32.887 | 108.452 | 175.974 | 18.023 | 12302962 | 0 |
| browser_lifecycle | 5 | 3 | 5 | 1 | 3.200 | 4.197 | 4.197 | 313.747 | 206475 | 3 |
| browser_lifecycle | 7 | 5 | 7 | 1 | 1.989 | 5.741 | 5.741 | 369.029 | 254835 | 5 |
| browser_lifecycle | 12 | 10 | 12 | 1 | 4.517 | 12.661 | 12.661 | 319.412 | 452629 | 10 |
| rate_limit_cooldown | 1 | 0 | 3 | 1 | 0.007 | 0.050 | 0.050 | 15511.891 | 1864 | 0 |
| release_paths | 1 | 2 | 4 | 2 | 0.917 | 4.598 | 4.598 | 583.712 | 66625 | 0 |
| idempotency_restart_recovery | 1 | 0 | 53 | 49 | 303.329 | 568.892 | 577.566 | 85.671 | 161480 | 0 |

## 功能合同

- 账号分页以 0、1、200、500 个假账号覆盖数据库层与 API 层；逐页检查每页上限和 has_next，没有改为一次返回全部账号。
- dense-pack 复用现有选择器；0/1/200/500 规模均无截断，结束后 pending 与 inflight 归零。
- fake browser runtime 从 0 个进程开始，依次覆盖 3、5、10 worker 上限；超过上限的任务排队，任务完成并 idle 后归零，新任务自动再唤醒。10 worker 样本中 max_live_workers=10，queued_count=1。
- 429 冷却、timeout、cancel、exception 与 repeated release 均释放已有资源；最终 browser 与 quota reservation 归零。
- 50 个并发同幂等键竞争只产生一次合成 submit。accepted/polling 模拟重启继续恢复；unknown 不重复 submit；duplicate_submit_count=0。
- 错误归因只使用公开分类，error_attribution_accuracy=1.0。

## 证据边界

2026-08-12 的既有真实验收已证明服务进程重启后能从加密数据库恢复并成功执行，以及服务存活时空闲 80 秒后浏览器子进程归零。本轮只补离线合成压力和恢复量化。

本轮未执行 Windows 整机重启、真实 Chrome 10 并发、真实 Flow 压测、长期无人值守、服务重启、部署或 Zeabur/Linux 验收；不将这些项目写作完成。
