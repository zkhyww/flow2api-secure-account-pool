# 2026-08-13 Account × Model Availability QC Fix Report

## Scope and constraints

This round repaired the QC findings against the existing dirty worktree at:

`D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo`

Constraints preserved throughout:

- kept all pre-existing dirty worktree changes;
- no commit, push, PR, deployment, restart, or real generation;
- no public model ID, scheduler architecture, or request-body contract expansion;
- account/model backfill reads task metadata only and never reads `prompt` or `result_urls`.

## RED → GREEN evidence

### 1. Real startup backfill and idempotency

RED added in `tests/test_account_model_availability.py` to reproduce the real existing-database sequence twice:

1. existing DB has historical `tasks` and no availability table;
2. startup calls `init_db()` first;
3. startup then calls `check_and_migrate_db()`;
4. repeat the same startup a second time.

Initial RED failed because the first real startup produced an empty availability table (`[]`): `init_db()` had already created the table, so migration skipped backfill.

GREEN implementation:

- migration now always calls the safe backfill after ensuring the table;
- backfill first snapshots existing `(token_id, model)` facts and only derives previously unseen pairs;
- historical rows for one pair are aggregated once, then inserted with `INSERT OR IGNORE`;
- repeated startup does not re-increment `successful_generations` or `explicit_denials`;
- task scan remains exactly `SELECT token_id, model, status, has_media, error_class FROM tasks` (plus ordering), with source-contract assertions that `prompt`, `result_urls`, and `SELECT * FROM tasks` are absent.

Focused GREEN after this repair: `5 passed`.

### 2. Public `/v1` error contract remains legacy-compatible

RED added in `tests/test_batch2_idempotency_boundaries.py` for public OpenAI-compatible non-stream and stream errors.

Observed RED:

- idempotent public request expected legacy 502 but received the new handler 403;
- non-idempotent public request expected legacy 500 but received the new handler 403;
- SSE error event exposed `error.code=model_access_denied` instead of legacy `generation_failed`.

Root cause: the earlier failure-attribution work intentionally made the shared generation handler expose stable internal/test-page classifications, but `/v1/chat/completions` consumed that shared payload directly.

GREEN implementation:

- retained stable handler classifications for diagnostics and `/api/test/...`;
- added a narrow `/v1/chat/completions` route-boundary compatibility normalizer;
- only newly exposed stable handler classes are normalized;
- public `error.code` is restored to `generation_failed`;
- idempotent public status preserves prior behavior (400 content policy, 402 quota, 409 submission uncertain, otherwise 502);
- non-idempotent newly classified handler exceptions remain legacy 500;
- streaming and non-streaming public paths are both covered;
- `/v1/models` and request formats remain untouched.

Focused GREEN for public/failure/admin tests: `18 passed, 9 subtests passed`.

### 3. Idempotent image/video `membership_tier` must record and stop for a fixed diagnostic account

RED added for both image and video using a fixed `diagnostic_token_id` and an upstream membership-tier rejection.

Observed RED: both tests timed out because the same diagnostic account was selected repeatedly after `continue`, and no unavailable fact was recorded.

GREEN implementation:

- explicit membership rejection is recorded before failover control flow;
- fixed diagnostic-account mode returns immediately after recording;
- any existing idempotent task is closed as failed/released before return;
- normal account-pool behavior still follows the pre-existing failover logic;
- the local tier gate in the idempotent image/video paths follows the same record-first rule.

Focused GREEN: `2 passed`.

An additional RED found the same missing fact in the shared non-idempotent tier gate: generation returned without upstream submission, but no red fact existed. The minimal repair records `membership_tier` before the existing return and leaves routing behavior unchanged. Final availability-file GREEN: `14 passed, 8 subtests passed`.

### 4. Video polling explicit denials must update account/model facts

RED added for all four combinations:

- normal video + `model_access_denied` during poll;
- idempotent video + `model_access_denied` during poll;
- normal video + `membership_tier` during poll;
- idempotent video + `membership_tier` during poll.

Observed RED: all four failed with no public model entry in `account_model_availability` even though the task was marked failed.

GREEN implementation:

- the original public model ID is carried only in the request-local `response_state`;
- the polling failure branch records the explicit denial against that public ID before failing the task;
- no task schema or scheduler architecture change was introduced.

Focused GREEN: `4 passed`.

### 5. Required backend behavior matrix

`tests/test_account_model_availability.py` now covers:

- normal image success → available;
- idempotent image success → available;
- normal video success → available;
- idempotent video success → available;
- submit-time `model_access_denied`;
- submit-time `membership_tier` for image and video fixed-diagnostic paths;
- poll-time `model_access_denied` for normal and idempotent video;
- poll-time `membership_tier` for normal and idempotent video;
- reCAPTCHA, quota, rate limit, and upstream 5xx do not create red facts;
- account isolation;
- later successful generation recovers an unavailable pair to available.

Final availability suite: `14 passed, 8 subtests passed`.

## Task 4 test-page tri-state UX

Page contract RED was added before changing `static/test.html`. Initial RED showed the page had no model-availability endpoint call, no availability grouping function, and no friendly model-name function.

GREEN implementation:

- availability endpoint: `/api/test/model-availability?diagnostic_token_id=...`;
- green label: `已验证可用`;
- red label: `当前账号不可用`;
- yellow label: `待验证`;
- recommendation grouping prefers all green models;
- when no green exists, all yellow models already marked `优先测试` are recommended;
- when there is no priority-yellow candidate, unknown/yellow models are used as the non-empty fallback;
- red and remaining yellow models are placed in collapsed `其他模型` and remain clickable;
- friendly display names are presentation-only;
- `item.dataset.model`, `STATE.selectedModel`, and request `body.model` keep the original model ID;
- changing `diagnosticTokenId` immediately reloads model facts;
- external API-key mode clears account facts, so the catalog renders as yellow/pending without blocking calls;
- no `diagnostic_token_id` is added to external API-key calls.

One page test expectation was corrected during GREEN because both existing 4s and 6s fast landscape variants are intentionally labeled `优先测试`; with no green, both belong in the yellow recommendation group.

Final page contract suite: `12 passed`.

## Verification gates

Fresh verification after implementation:

- focused: `44 passed, 17 subtests passed in 14.41s`;
- related regression: `72 passed, 9 subtests passed in 37.13s`;
- full pytest: `260 passed, 69 subtests passed in 72.54s`;
- `PYTHONPYCACHEPREFIX=C:\Windows\Temp\flow2api-account-model-qc venv/Scripts/python.exe -m compileall -q src`: exit 0;
- `git diff --check`: exit 0.

Git emitted existing LF→CRLF working-copy warnings for several dirty files, but `git diff --check` reported no whitespace errors.

## Codex 独立 QC 返修

Codex 独立审查在上述实现后发现两个 Important 数据库回归。本返修严格限制在这两项，没有扩展 API、调度、页面或生成行为。

### Important 1: 删除账号时 availability 外键阻止父 token 删除

RED added in `tests/test_account_model_availability.py` with a real SQLite database:

1. `init_db()`;
2. create one token (which also creates its existing `token_stats` row);
3. create an existing related project and task;
4. record one `account_model_availability` fact;
5. call the real `delete_token(token_id)`.

Observed RED:

- `delete_token()` failed at `DELETE FROM tokens WHERE id = ?` with `sqlite3.IntegrityError: FOREIGN KEY constraint failed`;
- this confirms `_configure_connection()` has `PRAGMA foreign_keys = ON` and the new availability row was the undeleted child.

Minimal GREEN:

- added exactly one child cleanup statement in `delete_token()`: `DELETE FROM account_model_availability WHERE token_id = ?` before deleting the parent token;
- existing request-log nulling and `tasks` / `token_stats` / `projects` deletion order/behavior otherwise remains unchanged;
- regression assertion verifies the token and availability facts are gone, and the pre-existing related `tasks`, `token_stats`, and `projects` rows are still removed.

### Important 2: extension plugin-session expiry index belonged to the wrong helper

RED added for both relevant boundaries:

- immediately after `init_db()`, `idx_extension_plugin_sessions_expires_at` must exist; current code failed with no index row in `sqlite_master`;
- on a minimal database containing `extension_plugin_sessions` but no `tasks`, `_backfill_account_model_availability()` returns early; current code therefore also left the expiry index absent.

Root cause: `CREATE INDEX IF NOT EXISTS idx_extension_plugin_sessions_expires_at ...` had been accidentally moved/indented to the end of `_backfill_account_model_availability()`, coupling an extension-session invariant to an unrelated task backfill path.

Minimal GREEN:

- moved the index creation back into `_ensure_extension_plugin_sessions_table()` directly after its table creation;
- removed index creation entirely from `_backfill_account_model_availability()`;
- source audit confirms `idx_extension_plugin_sessions_expires_at` now occurs exactly once in `src/core/database.py`;
- both `init_db()` and subsequent migration preserve the index, and the no-`tasks` backfill case no longer matters to that invariant.

### RED → GREEN and final gates for Codex QC返修

RED command:

`venv/Scripts/python.exe -m pytest tests/test_account_model_availability.py -q -k "delete_token_removes_model_availability or extension_session_expiry_index_exists"`

Observed RED after tightening all three contracts: `3 failed, 14 deselected`; failures were the FK `IntegrityError`, missing index immediately after `init_db()`, and missing index when availability backfill had no `tasks` table.

GREEN / verification:

- exact three new regressions: `3 passed, 14 deselected in 1.21s`;
- complete availability suite: `17 passed, 8 subtests passed in 15.57s`;
- focused availability + extension pairing: `25 passed, 8 subtests passed in 14.85s`;
- full pytest: `263 passed, 69 subtests passed in 71.74s`;
- `PYTHONPYCACHEPREFIX=C:/Windows/Temp/flow2api-account-model-codex-qc venv/Scripts/python.exe -m compileall -q src tests`: exit 0;
- `git diff --check`: exit 0 (existing LF→CRLF working-copy warnings only; no whitespace errors).

No real generation, commit, push, PR, deployment, or unrelated repair was performed.

## Terra 独立 QC 第二轮返修

This pass was restricted to the three Important findings from the second independent Terra read-only QC. No handler parameter, scheduler, model ID, request body, migration framework, or generation behavior was otherwise expanded.

### Important 1: old-style `generation_failed/502` could bypass the public `/v1` legacy 500 contract

RED was added at the real OpenAI-compatible route boundary with the same handler payload in both modes:

- non-idempotent `/v1/chat/completions` with `error.code=generation_failed` and `status_code=502` expected HTTP 500 but actually remained 502;
- the same payload with an idempotency key was locked to the existing HTTP 502 behavior.

The initial route RED run reported `2 failed, 8 passed, 3 subtests passed`; one failure was this exact 502-versus-500 mismatch.

Root cause: `_restore_legacy_openai_v1_error_contract()` only normalized codes in `_HANDLER_STABLE_ERROR_CLASSES`. Old handler branches that already emitted `generation_failed` therefore bypassed restoration even when their 502 status came from a non-idempotent generation failure.

Minimal GREEN:

- `generation_failed` is handled explicitly at the public route compatibility boundary;
- only `generation_failed + status_code=502 + no idempotency key` is restored to 500;
- the same 502 with an idempotency key remains 502;
- existing idempotent special statuses are explicitly locked by test: `content_policy=400`, `quota_exhausted=402`, `submission_uncertain=409`;
- `error.code` remains `generation_failed`.

Route GREEN after this change: `10 passed, 3 subtests passed`.

### Important 2: public `/v1` SSE leaked failure-reason frames before the final normalized error

A route-level streaming RED supplied three handler outputs in sequence:

1. an SSE `reasoning_content` failure frame containing the internal class `model_access_denied`;
2. an SSE failure frame containing an upstream-only marker and a full private HTTPS URL;
3. the final structured handler error.

Before the repair, the public SSE contained all of `model_access_denied`, the upstream marker, and the full URL even though the final error code was later normalized. This was the second failure in the initial `2 failed, 8 passed, 3 subtests passed` route RED run.

Minimal GREEN is intentionally confined to `_iterate_openai_stream(..., legacy_public_contract=True)`:

- handler SSE frames whose delta is an explicit `错误:` failure frame are suppressed before they reach public `/v1` clients;
- final public structured errors are restored to the legacy status/code contract and their streamed message is reduced to the safe literal `generation_failed`;
- normal progress frames are not globally removed;
- no generation-handler parameter or handler behavior was changed.

The same regression test then calls `/api/test/chat/completions` through the non-legacy stream path and asserts the allowlisted stable class `model_access_denied` is still visible there. Thus the public privacy boundary does not erase the diagnostic classification used by the authenticated test page.

Public stream GREEN evidence asserts the public text contains `generation_failed` and contains none of the internal class, upstream marker, private URL, or any `https://` fragment.

### Important 3: availability backfill materialized the entire historical `tasks` table in Python

A real DB counting RED inserted:

- 50 decisive historical tasks for `(token_id=41, existing-model)`, after a newer runtime availability fact already existed for that pair;
- 3 decisive tasks for a previously unseen `(token_id=42, new-model)` pair.

The probe wrapped only the cursor returned by the backfill `FROM tasks` query. Before the repair, Python received all 53 task rows, including the 50 rows belonging to the already-known pair. RED failed with `1 != 53`.

Minimal GREEN replaced the Python-side full-history aggregation with one SQL-side windowed aggregation:

- `NOT EXISTS` excludes account/model pairs that already have runtime facts before rows are returned to Python;
- SQL aggregates successful-generation and explicit-denial counters per missing pair;
- the latest decisive task state determines `available` versus `unavailable`, preserving the historical ordering semantics;
- Python now receives one aggregate row per missing pair and performs the existing `INSERT OR IGNORE` only;
- no migration marker or second migration mechanism was added.

The same 53-task fixture now returns exactly 1 row to Python, for `(42, new-model)`, while preserving:

- existing runtime fact `(41, existing-model)` unchanged at `available`, success count 1;
- new historical aggregate `(42, new-model)` as `available`, success count 2, explicit-denial count 1;
- repeated real startup idempotency;
- older backfill behavior and account isolation.

Sensitive-field boundary remains intact: the backfill source contains no `prompt`, no `result_urls`, and no `SELECT * FROM tasks`. Task payload fields used for reconstruction remain `token_id`, `model`, `status`, `has_media`, and `error_class`; task `id` is used only inside SQL as the ordering key and is not returned/materialized in Python.

Performance boundary: SQLite may still inspect historical task rows to determine which missing facts need reconstruction, but already-known account/model rows are filtered inside SQL and Python materialization/memory is reduced from O(all historical task rows) to O(missing account/model pairs).

### RED → GREEN and final gates for Terra second-round QC

RED evidence:

- public route RED: `2 failed, 8 passed, 3 subtests passed`; failures were the non-idempotent old-style 502 status and public SSE leakage;
- backfill performance RED: `1 failed, 17 deselected`; the assertion showed 53 task rows returned to Python instead of 1.

GREEN / verification:

- route contracts: `10 passed, 3 subtests passed in 1.40s`;
- backfill performance + real startup idempotency + sensitive-field boundary: `3 passed, 15 deselected in 2.11s`;
- focused availability + public route + admin test capability + failure-class suite: `41 passed, 20 subtests passed in 13.59s`;
- full pytest: `267 passed, 72 subtests passed in 51.13s`;
- `PYTHONPYCACHEPREFIX=C:/Windows/Temp/flow2api-account-model-terra-qc venv/Scripts/python.exe -m compileall -q src tests`: exit 0;
- `git diff --check`: exit 0 (existing LF→CRLF working-copy warnings only; no whitespace errors).

No real generation, commit, push, PR, deployment, or unrelated repair was performed in this Terra second-round QC pass.

## Files changed by this QC repair

This round directly edited:

- `src/core/database.py`
- `src/api/routes.py`
- `src/services/generation_handler.py`
- `static/test.html`
- `tests/test_account_model_availability.py`
- `tests/test_batch2_idempotency_boundaries.py`
- `tests/test_test_page_contract.py`
- `docs/superpowers/reports/2026-08-13-account-model-availability-qc-fix.md`

Other dirty files visible in `git status` pre-dated this QC round and were preserved rather than reverted or rewritten.

## Residual risks / boundaries

- Backfill deliberately skips an account/model pair once a fact row already exists. This is what makes repeated startup idempotent and prevents historical tasks from overwriting newer runtime truth; counters are therefore a one-time historical aggregate only for previously unseen pairs.
- Public compatibility normalization is intentionally scoped to `/v1/chat/completions`; diagnostic `/api/test/...` continues to expose stable failure classes. Gemini endpoints and `/v1/models` were not expanded by this feature.
- The page behavior is verified through Python contract tests plus Node execution of grouping logic. No live generation/browser smoke was performed because this round explicitly forbids real generation.
- No commit, push, PR, deployment, or real generation was performed.
