# Flow2API Pluginless Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use test-driven development and execute this plan task-by-task. Every production change must be preceded by an observed failing focused test.

**Goal:** Make Flow2API persist Google accounts across service/Windows restarts and generate through its on-demand personal browser without requiring the Chrome extension.

**Architecture:** Repair the existing per-token browser-cookie bridge, replace extension-assisted onboarding with a native personal-browser capture/import path, and complete idle runtime shutdown. Reuse the existing token table, DPAPI protection, scheduler, browser worker pool, request fingerprint binding, observability, and account management.

**Tech Stack:** Python 3, FastAPI, SQLite/aiosqlite, nodriver/CDP, vanilla HTML/JavaScript, pytest/unittest.

## Global Constraints

- Preserve all existing uncommitted changes and do not modify `D:\codex项目\Framefield-Studio`.
- Never expose credential values, browser profiles, complete URLs, prompts, or media in logs, tests, reports, or browser messages.
- Do not introduce a second credential store or account scheduler.
- Keep the extension only as an optional advanced fallback; it must not gate personal mode readiness.
- Do not push, open issues/PRs, publish, deploy, or reboot Windows.
- Finish with full verification and one clean local commit only.

---

### Task 1: Repair the persisted account-to-browser bridge

**Files:**
- Modify: `src/services/browser_captcha_personal.py`
- Create: `tests/test_personal_cookie_persistence.py`

**Interfaces:**
- `_load_token_cookie(token_id) -> Optional[str]` reads `Token.google_cookies`.
- `_persist_context_cookies_to_token(...) -> bool` updates `google_cookies` through `Database.update_token`.

- [ ] Write tests using a real temporary Database proving load returns the existing `google_cookies`, writeback updates that field, an absent cookie returns false without cross-account fallback, and the raw SQLite cell remains protected.
- [ ] Run `venv\Scripts\python.exe -m pytest tests/test_personal_cookie_persistence.py -q` and record RED caused by `token.cookie`/`cookie=`.
- [ ] Change only the two incorrect field references and credential-free log labels.
- [ ] Re-run the focused test and `tests/test_batch4_credential_persistence.py` to GREEN.

### Task 2: Lock account isolation and restart restoration

**Files:**
- Modify: `tests/test_browser_captcha_personal.py`
- Create: `tests/test_personal_restart_restore.py`
- Modify only if RED proves necessary: `src/services/browser_captcha_personal.py`

**Interfaces:**
- Every selected `token_id` gets a context whose cookie signature belongs to that token.
- A fresh `BrowserCaptchaService` instance can reload persisted state without extension/session pairing state.

- [ ] Add tests for two tokens with distinct opaque cookie fixtures, context injection attribution, resident reuse for the same token, context replacement for a different token, and a new service instance after simulated restart.
- [ ] Observe RED/GREEN honestly; do not change production code for already-working behavior.
- [ ] If needed, minimally fix token-affinity cleanup or binding order and rerun browser lifecycle/pool tests.

### Task 3: Replace extension-assisted onboarding with native capture

**Files:**
- Modify: `src/services/account_onboarding.py`
- Modify: `src/services/browser_captcha_personal.py`
- Modify: `src/api/admin.py`
- Modify: `static/manage.html`
- Modify: `tests/test_browser_account_onboarding.py`
- Create: `tests/test_pluginless_account_onboarding.py`

**Interfaces:**
- A headed isolated onboarding browser returns a private in-process result containing normalized session state; public polling remains allowlisted.
- Existing `TokenManager.add_token/update_token` remains the only account persistence entry.

- [ ] Replace the test that expects an ephemeral paired extension with tests proving no extension directory/bootstrap/pairing service is used.
- [ ] Add tests for login detection, account add/update, duplicate-click idempotency, timeout cleanup, and credential-free public status.
- [ ] Observe focused RED before implementation.
- [ ] Add a browser method that waits for a completed Flow login, gathers cookies from its isolated context, derives only the fields required by the existing token import path, and returns them directly in process without logging them.
- [ ] Update admin onboarding launcher to use this native result, persist through TokenManager, then close the browser.
- [ ] Keep manual token entry and the extension import endpoint as advanced fallback only.
- [ ] Run onboarding, plugin import, credential persistence, and admin contract tests.

### Task 4: Complete on-demand startup and full idle shutdown

**Files:**
- Modify: `src/services/browser_captcha_personal.py`
- Modify: `tests/test_batch4_browser_lifecycle.py`
- Create: `tests/test_personal_runtime_autoscale.py`

**Interfaces:**
- First task lazily starts a worker.
- Idle reaper closes stale tabs and, after no active/resident/custom work remains, closes the browser runtime.
- Worker count stays between 1 and 10 when active and can return to zero live processes when idle.

- [ ] Add clock-controlled tests proving no startup at service boot, startup on first solve, no shutdown while work is active, final runtime shutdown after TTL, and successful restart on the next solve.
- [x] Add pool tests for minimum 3 configured slots, dense reuse, growth under pressure, and max 10 workers.
- [ ] Observe RED where the browser remains alive after the last tab.
- [ ] Call the existing `shutdown_idle_runtime_if_needed()` from the idle reaper after tab reclamation, without adding another lifecycle manager.
- [ ] Run browser lifecycle, dense-pack, three-account routing, and concurrency suites.

### Task 5: Make personal mode the normal UI path

**Files:**
- Modify: `static/manage.html`
- Modify: `static/test.html`
- Modify: `src/api/admin.py`
- Create: `tests/test_pluginless_manage_contract.py`

**Interfaces:**
- Personal mode shows service/browser/account readiness, not extension connectivity.
- Extension controls remain under an explicitly labelled advanced fallback section.

- [ ] Add static/API contract tests proving personal mode never reports plugin-required, onboarding copy says login persists, and extension red/yellow status is hidden outside extension mode.
- [ ] Observe RED, then minimally adjust the existing status selector and UI copy.
- [ ] Preserve port 8000, existing admin-session test flow, and all three accounts' enabled state.
- [ ] Run management, test-page, extension fallback, and admin observability tests.

### Task 6: Independent verification and local commit

**Files:**
- Update: `docs/FLOW2API_LOCAL_ACCOUNT_POOL_VALIDATION.md`

- [ ] Review `git diff` for unrelated changes, credential literals, debug output, and accidental changes outside this repository.
- [ ] Run focused pluginless tests and then `venv\Scripts\python.exe -m pytest -q`.
- [ ] Run JavaScript syntax checks, static/contract tests, credential-pattern scans reporting counts only, and `git diff --check`.
- [ ] Restart only the Flow2API process and verify public status shows three persisted accounts with no plugin readiness requirement.
- [ ] Perform one low-cost, one-image, non-sensitive personal-mode smoke; record only selected token id, stage, status, attempt count, and has-media.
- [x] If automated and real validation pass, create one clean local commit. Do not push.
