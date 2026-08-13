# Account Model Availability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and execute each task RED→GREEN.

**Goal:** 在不改变现有调用协议和调度的前提下，自动记录每个账号的真实模型可用性，并在测试页提供清晰三态选择。

**Architecture:** 使用独立 SQLite 表作为“账号 × 公开模型 ID”的事实源，由生成处理器在真实成功或明确权限拒绝时更新。管理测试端点只返回白名单状态，测试页按所选诊断账号分组展示，提交仍使用原模型 ID。

**Tech Stack:** Python 3.11、FastAPI、aiosqlite、原生 HTML/JavaScript、unittest/pytest。

## Global Constraints

- 不修改 `D:\codex项目\Framefield-Studio`。
- 不改变公共 `/v1` API、模型 ID、账号池选择和并发逻辑。
- 不保存或输出凭据、提示词、媒体 URL、完整上游响应。
- 不为探测状态额外提交真实任务。
- 不 commit、push、PR 或部署；由 Codex 最终验收后决定本地提交。

---

### Task 1: 持久化账号模型事实

**Files:**
- Modify: `src/core/database.py`
- Test: `tests/test_account_model_availability.py`

**Interfaces:**
- `record_account_model_available(token_id: int, model: str) -> None`
- `record_account_model_unavailable(token_id: int, model: str, error_class: str) -> None`
- `get_account_model_availability(token_id: int) -> dict[str, dict]`

- [ ] 写 RED：迁移幂等、账号隔离、成功覆盖拒绝、临时错误无写入、字段白名单。
- [ ] 运行 `venv/Scripts/python.exe -m pytest tests/test_account_model_availability.py -q`，确认因表/接口不存在而失败。
- [ ] 最小实现独立表和三个数据库接口；仅允许两种明确拒绝错误类。
- [ ] 增加仅使用 tasks 元数据的安全幂等回填。
- [ ] 重跑聚焦测试至 GREEN。

### Task 2: 真实生成结果自动更新

**Files:**
- Modify: `src/services/generation_handler.py`
- Test: `tests/test_account_model_availability.py`

**Interfaces:**
- 成功且有媒体：调用 `record_account_model_available(token.id, model)`。
- `model_access_denied` / `membership_tier`：调用 `record_account_model_unavailable(...)`。
- 其余结果不更新状态。

- [ ] 写 RED：图片成功、视频成功、明确拒绝、reCAPTCHA/额度/5xx 不误判，覆盖非幂等与现有配额路径。
- [ ] 运行聚焦测试并确认预期失败。
- [ ] 在统一结果边界增加最小记录辅助函数，不复制新调度逻辑。
- [ ] 重跑聚焦测试至 GREEN。

### Task 3: 管理测试状态查询

**Files:**
- Modify: `src/api/routes.py` 或现有管理测试路由所属文件
- Test: `tests/test_admin_test_capability.py`

**Interfaces:**
- 新增管理会话保护的状态查询，输入 `diagnostic_token_id`，输出仅含 `model/status/error_class/last_verified_at` 等白名单。
- 公共 `/v1/models` 响应保持逐字合同兼容。

- [ ] 写 RED：未登录拒绝、账号不存在拒绝、白名单输出、公共端点不变。
- [ ] 运行聚焦测试确认失败。
- [ ] 最小实现查询端点。
- [ ] 重跑聚焦测试至 GREEN。

### Task 4: 测试页三态与易懂名称

**Files:**
- Modify: `static/test.html`
- Test: `tests/test_test_page_contract.py`

**Interfaces:**
- `loadModelAvailability(tokenId)` 获取当前账号状态。
- `getModelAvailability(modelId)` 返回 `available|unavailable|unknown`。
- 页面显示友好名称，DOM 的 `data-model` 和请求 body 始终保留原模型 ID。

- [ ] 写 RED：三色标签、默认推荐视图、无绿色时黄色推荐回退、其他模型可展开、切换账号刷新、请求 ID 不变。
- [ ] 运行页面合同测试确认失败。
- [ ] 最小实现分组、筛选和友好名称。
- [ ] 重跑页面合同测试至 GREEN。

### Task 5: 独立验收

- [ ] 运行所有新增及相关聚焦测试。
- [ ] 运行 `venv/Scripts/python.exe -m pytest -q`。
- [ ] 运行 `venv/Scripts/python.exe -m compileall -q src tests`。
- [ ] 运行 `git diff --check`。
- [ ] 重启本地 8000 服务，执行不耗额度的页面/API 合同验收。
- [ ] 仅在自动证据不足且确有必要时才讨论真实生成；本功能本身不需要额外 smoke。

