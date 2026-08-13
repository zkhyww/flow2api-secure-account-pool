# Flow2API Usability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make local multi-account onboarding, testing, and 2K image upsampling safe, low-friction, and independently verifiable without changing Flow2API's account-pool architecture.

**Architecture:** Reuse the existing admin session, token manager, extension captcha router, personal-browser lifecycle, and generation pipeline. Add narrowly scoped pairing/test-capability contracts and carry an allowlisted captcha fingerprint bundle through the existing request context.

**Tech Stack:** Python 3, FastAPI, SQLite, asyncio, vanilla HTML/JavaScript, Chrome Manifest V3, pytest/unittest.

## Global Constraints

- Do not modify `D:\codex项目\Framefield-Studio` or any of its worktrees.
- Do not push, open issues/PRs, publish, deploy, or migrate production data.
- Never read, print, package, or persist Cookies, Profiles, passwords, tokens, complete media URLs, or raw prompts in diagnostics or artifacts.
- Preserve existing Flow2API account-pool, scheduler, concurrency, cooldown, circuit-breaker, privacy, logging, and observability architecture.
- Every production behavior must have a failing automated test observed before implementation.
- Real smoke tests may use one non-sensitive image and must report only allowlisted status fields.

---

### Task 1: Fix management-page loading contract

**Files:**
- Modify: `static/manage.html`
- Test: `tests/test_manage_page_bootstrap.py`

**Interfaces:**
- Produces: a defined `checkAuth(): Promise<boolean>` bootstrap and explicit `loading | ready | error` token-list state.

- [ ] Write tests asserting `checkAuth` exists before the DOMContentLoaded handler, failed token fetch does not render zero accounts, and a visible retry control is rendered.
- [ ] Run `venv\Scripts\python.exe -m pytest tests/test_manage_page_bootstrap.py -q` and capture the expected failures.
- [ ] Implement the smallest bootstrap and render-state changes in `static/manage.html`.
- [ ] Re-run the focused test and existing `tests/test_batch5_manage_contract.py`.
- [ ] Commit only this task as `fix: make management bootstrap failures visible`.

### Task 2: Add one-click browser account onboarding

**Files:**
- Create: `src/services/account_onboarding.py`
- Modify: `src/api/admin.py`
- Modify: `src/services/browser_captcha_personal.py`
- Modify: `static/manage.html`
- Test: `tests/test_browser_account_onboarding.py`

**Interfaces:**
- Produces: `AccountOnboardingService.start() -> PublicOnboardingState`, `status(session_id)`, and `finish(session_id, outcome)`.
- Public state fields are limited to `session_id`, `stage`, `status`, `started_at`, `expires_at`, `account_count_before`, `account_count_after`, and `error_class`.

- [ ] Write focused tests for one active onboarding session, duplicate-click idempotency, one browser launch, timeout, success detection by account-count change, and response-field allowlisting.
- [ ] Run the focused tests and observe RED caused by missing service/API/button.
- [ ] Implement an in-memory short-lived onboarding state machine and admin-session-protected start/status endpoints.
- [ ] Reuse the existing visible-browser lifecycle; add an explicit extension directory parameter instead of changing global captcha mode.
- [ ] Add the management button, disabled/running state, polling, and plain-language status messages.
- [ ] Run focused tests plus browser lifecycle and admin contract tests.
- [ ] Commit as `feat: add one-click browser account onboarding`.

### Task 3: Pair the Yingce extension without a global API key

**Files:**
- Modify: `extension/manifest.json`
- Modify: `extension/background.js`
- Modify: `extension/options.html`
- Modify: `extension/options.js`
- Modify: `src/api/admin.py`
- Modify: `src/api/routes.py`
- Create: `src/services/extension_pairing.py`
- Test: `tests/test_extension_pairing.py`
- Test: `tests/test_extension_contract.py`

**Interfaces:**
- Produces: a one-use pairing exchange returning a revocable opaque plugin session; WebSocket auth accepts plugin session in a header/subprotocol, never a URL query.
- Extension capability marker: `yingce-flow2api-worker-v1`.

- [ ] Add tests proving one-use expiry, replay rejection, revocation, no global API key in URL/logs/storage, profile-stable route identity, and correct customized-extension marker.
- [ ] Run focused tests and observe RED.
- [ ] Implement pairing storage with bounded TTL and constant-time token checks; never return the global API key.
- [ ] Update the customized extension to consume bootstrap configuration, store only its revocable session and public instance metadata, and preserve manual/current-account import.
- [ ] Make browser launch load the customized extension automatically and pass only the one-time bootstrap handle.
- [ ] Run extension contract, plugin import, WebSocket routing, and onboarding tests.
- [ ] Commit as `feat: pair customized extension automatically`.

### Task 4: Remove repeated API-key entry from the test page

**Files:**
- Modify: `src/api/admin.py`
- Modify: `src/api/routes.py`
- Modify: `static/test.html`
- Test: `tests/test_admin_test_capability.py`
- Test: `tests/test_test_page_contract.py`

**Interfaces:**
- Produces: an admin-session-bound short-lived test capability accepted only on same-origin test endpoints; optional `diagnostic_token_id` is admin-only and never part of public API behavior.

- [ ] Add tests for automatic same-origin capability, expiry/logout invalidation, manual API-key fallback, no persistent key storage, and unauthorized diagnostic account selection rejection.
- [ ] Run focused tests and observe RED.
- [ ] Implement capability issuance/verification without exposing the global API key to JavaScript.
- [ ] Move manual API-key controls under advanced settings and auto-load models through the capability.
- [ ] Add an admin-only diagnostic account selector populated with public account summaries.
- [ ] Run focused API/page tests and existing OpenAI/Gemini route tests.
- [ ] Commit as `feat: streamline authenticated model testing`.

### Task 5: Preserve captcha identity during image upsampling

**Files:**
- Modify: `extension/background.js`
- Modify: `src/services/browser_captcha_extension.py`
- Modify: `src/services/flow_client.py`
- Modify: `src/services/generation_handler.py`
- Create: `tests/test_extension_captcha_fingerprint.py`
- Create: `tests/test_image_upsample_retry_contract.py`

**Interfaces:**
- Produces: `ExtensionCaptchaBundle(token, fingerprint)` with only `user_agent`, `accept_language`, `sec_ch_ua`, `sec_ch_ua_mobile`, and `sec_ch_ua_platform` in `fingerprint`.
- `upsample_image(..., token_id, session_id)` performs at most `flow_max_retries` total upstream submissions; callers do not retry it again.

- [ ] Add tests proving the extension bundle is route-bound and allowlisted, the request fingerprint reaches `_make_request`, every retry obtains a new captcha token, and three configured retries cause exactly three upstream submissions rather than nine.
- [ ] Add a generation-handler test proving final upsample failure returns the original image with `delivery_mode=original_fallback` and does not claim 2K success.
- [ ] Run both focused tests and observe RED for missing bundle propagation and duplicate retry.
- [ ] Implement structured extension responses and reject unexpected fingerprint keys.
- [ ] Preserve the fingerprint for the matching Flow request and remove the outer upsample retry loop.
- [ ] Keep fallback behavior but expose an accurate public stage/result marker.
- [ ] Run focused tests plus `tests/test_api_captcha_fingerprint.py` and generation lifecycle tests.
- [ ] Commit as `fix: preserve captcha identity for image upsampling`.

### Task 6: Three-account routing and concurrency regression

**Files:**
- Modify only if RED proves a defect: `src/services/load_balancer.py`, `src/services/concurrency_manager.py`
- Create: `tests/test_three_account_routing.py`

**Interfaces:**
- Consumes existing account selection and concurrency APIs; produces no new scheduler.

- [ ] Add tests for two active plus one inactive account, round-robin order, diagnostic pinning, per-account concurrency saturation, fallback to another active account, and reservation release after failure.
- [ ] Run tests and record whether current code is already GREEN; do not change production code if it is.
- [ ] If RED, implement only the proven scheduler defect and rerun batch 2-5 pool tests.
- [ ] Commit only if production behavior changed.

### Task 7: Security, full verification, and real smoke

**Files:**
- Update: `docs/FLOW2API_LOCAL_ACCOUNT_POOL_VALIDATION.md`

**Interfaces:**
- Produces a credential-free validation report and one clean local integration commit or a documented sequence of clean task commits.

- [ ] Run credential-pattern scans over tracked source and the prepared extension package; report counts only.
- [ ] Run `venv\Scripts\python.exe -m pytest -q` and all JavaScript/static contract subtests.
- [ ] Verify management-page refresh, one-click onboarding idempotency, plugin auto-configuration, and test-page no-key flow locally.
- [ ] With the user's existing three-account state, perform one diagnostic single-image base-generation smoke on the chosen account.
- [ ] Only after Task 5 tests are GREEN, perform one 2K single-image smoke and record `selected_token_id`, `attempt_count`, `status`, `has_media`, and `delivery_mode` only.
- [ ] Confirm any temporarily disabled account remains in the state chosen by the user; do not silently enable/disable accounts.
- [ ] Update validation documentation with exact commands and results, check `git diff --check`, and create the final local commit. Do not push.

## Out of Scope Until This Plan Passes

- Modifying or integrating `D:\codex项目\Framefield-Studio`.
- Claiming Yingce has generated media through Flow2API.
- Zeabur/cloud deployment.
- Replacing Flow2API's scheduler, database, privacy, logging, or account-pool architecture.
