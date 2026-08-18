# Durable Account Session Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each enabled Flow2API account recover its login from an isolated persistent local browser profile without extensions, while preserving the existing account pool, concurrency, logging, and browser lifecycle behavior.

**Architecture:** Keep `tokens` as the only account source of truth and add a small authentication-recovery state machine plus an opaque per-account profile key. Normal image/video generation continues to use the existing temporary personal-browser workers; only onboarding, explicit re-login, or failed protocol refresh starts the matching persistent profile, captures refreshed session material, writes it through existing encrypted database paths, and closes the browser in `finally`.

**Tech Stack:** Python 3.8+, FastAPI, Pydantic 2, aiosqlite, nodriver/Playwright-backed personal browser service, vanilla HTML/JavaScript, pytest/unittest.

## Global Constraints

- Do not modify `D:\codex项目\Framefield-Studio`, gflow, the extension, or Yingce security controls.
- Do not store or log passwords, passkeys, verification codes, raw cookies, tokens, profile paths, full URLs, prompts, media, or response bodies.
- `is_active` means user scheduling intent only; authentication failures must never call `disable_token()`.
- Persistent profiles are local runtime state under `data/account_profiles/<opaque-key>` and must be excluded from Git, ZIP, logs, exports, and API responses.
- All personal-browser instances must share one maximum-10 live browser-process lease from immediately before launch until shutdown `finally`; keep the existing launch-parallelism gate separately for startup spike control. The ordinary pool, onboarding, re-login, and recovery all use the same live-process lease, so account count must not imply browser process count.
- Existing dense-pack scheduling, learned concurrency, circuit breakers, quota handling, media parsing, temporary worker cleanup, and idle reaping remain authoritative.
- Login, CAPTCHA, passkey, and two-step verification remain user-interactive; no bypass or credential collection.
- Production code changes must follow RED then GREEN; do not run paid real generation until synthetic tests pass.

---

### Task 1: Persist Authentication Recovery State Without Leaking It

**Files:**
- Modify: `src/core/models.py:8-60`
- Modify: `src/core/database.py:698-734`
- Modify: `src/core/database.py:898-934`
- Modify: `src/core/database.py:1231-1273`
- Test: `tests/test_account_session_recovery.py`

**Interfaces:**
- Produces `Token.account_profile_key: str`, `Token.auth_state: Literal["ok", "refresh_pending", "backoff", "reauth_required"]`, `Token.auth_failure_count: int`, `Token.auth_next_retry_at: Optional[datetime]`, and `Token.last_auth_error_class: str`.
- Produces `Database.get_auth_recovery_candidates(now: datetime) -> List[Token]` selecting only `is_active=1 AND auto_refresh_enabled=1` and excluding unexpired backoff rows.
- Produces `Database.update_token_auth_state(token_id: int, *, state: str, failure_count: int, next_retry_at: Optional[datetime], error_class: str) -> None` with a state/error-class allowlist.

- [ ] **Step 1: Write failing migration and query tests**

  Create `tests/test_account_session_recovery.py` cases that initialize a temporary SQLite database and assert the five columns exist with defaults, enabled expired accounts are returned, manually disabled accounts are omitted, unexpired backoff is omitted, and expired backoff is returned.

- [ ] **Step 2: Run tests to verify RED**

  Run: `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_account_session_recovery.py -q`

  Expected: FAIL because the new Token fields and database methods do not exist.

- [ ] **Step 3: Add schema, model fields, insertion values, and allowlisted updates**

  Add the columns to both fresh schema and incremental migration. Generate no profile key during migration; existing rows remain empty until explicit re-login. Reject unknown auth states/error classes before SQL update, and keep `update_token()` compatible with existing callers.

- [ ] **Step 4: Run focused tests to GREEN**

  Run the Task 1 command and require all tests to pass.

- [ ] **Step 5: Commit the independently testable data layer**

  Run: `git add src/core/models.py src/core/database.py tests/test_account_session_recovery.py && git commit -m "feat: persist account recovery state"`

### Task 2: Add an Opaque, Contained Per-Account Profile Store

**Files:**
- Create: `src/services/account_profile_store.py`
- Modify: `.gitignore`
- Test: `tests/test_account_profile_store.py`

**Interfaces:**
- Produces `AccountProfileStore(root: Path)`.
- Produces `create_key() -> str`, `resolve(key: str, *, create: bool = False) -> Path`, `exists(key: str) -> bool`, `clone_to_new_key(source_key: str) -> str`, and `remove(key: str) -> None`.
- Keys are lowercase UUID hex only; resolved paths must remain below the configured root after `resolve()` and symlink checks. Cloning rejects linked escape content and never mutates the source Profile. Removal first persists an opaque-key cleanup marker inside the Profile root; if filesystem cleanup is temporarily blocked, the marker survives and the next candidate creation retries it, so a failed delete is tracked rather than becoming an unbounded orphan.

- [ ] **Step 1: Write containment RED tests**

  Test unique keys, stable restart resolution, cross-account separation, rejection of empty/absolute/`..`/separator keys, rejection of symlink escape, and no email/token text in generated directory names.

- [ ] **Step 2: Run tests to verify RED**

  Run: `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_account_profile_store.py -q`

  Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the minimal profile store and ignore rule**

  Root it at `data/account_profiles`, use `uuid.uuid4().hex`, create directories only when explicitly requested, fail closed on containment uncertainty, and add `/data/account_profiles/` to `.gitignore`.

- [ ] **Step 4: Run focused tests to GREEN**

  Run the Task 2 command and require all tests to pass.

- [ ] **Step 5: Commit the profile boundary**

  Run: `git add .gitignore src/services/account_profile_store.py tests/test_account_profile_store.py && git commit -m "feat: isolate persistent account profiles"`

### Task 3: Stop Authentication Failures From Permanently Disabling Accounts

**Files:**
- Modify: `src/services/token_manager.py:18-28`
- Modify: `src/services/token_manager.py:564-592`
- Modify: `src/services/token_manager.py:734-797`
- Modify: `src/services/token_manager.py:860-900`
- Test: `tests/test_account_session_recovery.py`

**Interfaces:**
- Produces `TokenManager._mark_auth_success(token_id: int) -> None`.
- Produces `TokenManager._mark_auth_failure(token_id: int, error_class: str, *, interactive: bool) -> None` using bounded exponential backoff for transient failures.
- Keeps existing per-token single-flight `_refresh_futures`/locks as the only refresh deduplication mechanism.

- [ ] **Step 1: Add RED tests for failure semantics and single flight**

  Assert complete ST/AT/Cookie failure leaves `is_active=True`, transient failures set `backoff`, interactive failures set `reauth_required`, retry time increases but is bounded, success clears counters, and concurrent refresh calls execute one upstream attempt.

- [ ] **Step 2: Run the new tests to verify RED**

  Run: `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_account_session_recovery.py -q`

  Expected: FAIL at the current `disable_token()` behavior.

- [ ] **Step 3: Replace auth-driven disable with state transitions**

  Preserve 429/quota/user disable paths. Restrict auth error classes to stable values such as `network`, `oauth_callback_missing`, `cookie_rejected`, `profile_missing`, `interactive_verification`, and `browser_start_failed`; never persist raw exception text.

- [ ] **Step 4: Switch the protocol refresher to recovery candidates**

  Use `get_auth_recovery_candidates()` instead of `get_active_tokens()` only in the authentication refresher. Do not change generation routing eligibility.

- [ ] **Step 5: Run focused and existing token tests to GREEN**

  Run: `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_account_session_recovery.py tests/test_token_credential_persistence.py tests/test_batch3_failure_routing.py -q`

- [ ] **Step 6: Commit auth-state behavior**

  Run: `git add src/services/token_manager.py tests/test_account_session_recovery.py && git commit -m "fix: keep enabled accounts recoverable"`

### Task 4: Preserve and Reuse One Browser Profile Per Account

**Files:**
- Modify: `src/services/browser_captcha_personal.py:1560-1740`
- Modify: `src/services/browser_captcha_personal.py:12023-12110`
- Test: `tests/test_browser_captcha_personal.py`
- Test: `tests/test_pluginless_account_onboarding.py`

**Interfaces:**
- Extends `BrowserCaptchaService(..., persistent_profile_dir: Optional[Path] = None)`.
- Extends `capture_account_onboarding_result()` public result with only the opaque `account_profile_key`; no filesystem path is returned.
- Produces `capture_account_recovery_result(expected_email: str) -> Dict[str, Any]` that rejects identity mismatch and always closes browser-owned resources.

- [ ] **Step 1: Add RED lifecycle tests**

  Assert onboarding uses a supplied persistent profile without incognito flags, close preserves its directory, a new service instance resolves the same profile, temporary-worker cleanup does not remove it, identity mismatch fails closed, and success/failure/timeout all leave zero recovery browser instances.

- [ ] **Step 2: Run browser tests to verify RED**

  Run: `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_browser_captcha_personal.py tests/test_pluginless_account_onboarding.py -q`

- [ ] **Step 3: Add explicit persistent-profile mode**

  Keep the default temporary behavior unchanged. When `persistent_profile_dir` is supplied, pass it as the browser user-data directory, do not schedule it for temporary cleanup, and still close the process in `finally`.

- [ ] **Step 4: Add recovery capture with identity verification**

  Reuse the existing Flow login/session capture path, compare normalized captured email with `expected_email`, and return only stable success/error classification plus refreshed credential material to the private caller.

- [ ] **Step 5: Run browser focused tests to GREEN**

  Run the Task 4 command and require all tests to pass.

- [ ] **Step 6: Commit persistent browser behavior**

  Run: `git add src/services/browser_captcha_personal.py tests/test_browser_captcha_personal.py tests/test_pluginless_account_onboarding.py && git commit -m "feat: reuse per-account browser profiles"`

### Task 5: Wire Onboarding, Automatic Recovery, and Explicit Re-login

**Files:**
- Modify: `src/api/admin.py:562-636`
- Modify: `src/api/admin.py:848-970`
- Modify: `src/api/admin.py:1070-1138`
- Modify: `static/manage.html:977-1017`
- Test: `tests/test_browser_account_onboarding.py`
- Test: `tests/test_pluginless_manage_contract.py`
- Test: `tests/test_account_session_recovery.py`

**Interfaces:**
- `POST /api/admin/account-onboarding` allocates a profile key before launch and stores it only after successful identity capture.
- Produces `POST /api/tokens/{token_id}/reauth` for explicit account-scoped re-login and enable intent.
- Token-list responses expose `auth_status`, `auth_retry_after_seconds`, and `can_reauth`; they never expose profile key/path or credentials.

- [ ] **Step 1: Add RED API/UI contract tests**

  Assert new onboarding stores a profile key, mismatch does not overwrite the record, explicit re-login clones the target account's existing Profile into an isolated candidate and uses an empty candidate only when no Profile exists, successful identity verification replaces the reference through one atomic reauth transaction, transaction failure rolls back the Profile reference/auth/enable update and removes the candidate, failed/mismatched re-login preserves the old reference, re-login requires admin auth, public status is allowlisted, no profile path/key appears in API or HTML, and the account row shows “重新登录并启用” only when appropriate.

- [ ] **Step 2: Run API/UI tests to verify RED**

  Run: `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_browser_account_onboarding.py tests/test_pluginless_manage_contract.py tests/test_account_session_recovery.py -q`

- [ ] **Step 3: Wire profile-aware onboarding and re-login**

  Allocate with `AccountProfileStore`. On explicit re-login, clone an existing target Profile to an isolated candidate before opening the headed browser; if the target has no Profile, create an empty candidate. Require matching identity before any account-row write, then commit credentials, Profile reference, auth-success metadata and `is_active=True` for the explicit re-login-and-enable action in one SQLite transaction. A transaction failure must roll back the reference and all account-row changes before the candidate is cleaned; after a successful commit remove the superseded Profile. Any filesystem delete blocked by a temporary lock must retain a durable cleanup marker for retry on the next candidate creation. Close the browser in `finally`.

- [ ] **Step 4: Wire automatic headless recovery**

  After protocol Cookie refresh fails and a profile exists, acquire the existing per-token refresh lock, start the persistent profile through the shared live browser-process lease (while retaining the separate startup-parallelism gate), capture refreshed session material, update encrypted fields and auth state, and close the browser. Classify interactive login as `reauth_required`; classify temporary runtime failure as `backoff`.

- [ ] **Step 5: Update management status and action**

  Display `正常`, `等待自动恢复`, `稍后重试`, `需要重新登录`, or `已停用`. Keep add-account and manual-token functions intact. Poll only opaque session IDs and allowlisted stages.

- [ ] **Step 6: Run focused tests to GREEN**

  Run the Task 5 command and require all tests to pass.

- [ ] **Step 7: Commit the end-to-end recovery surface**

  Run: `git add src/api/admin.py static/manage.html tests/test_browser_account_onboarding.py tests/test_pluginless_manage_contract.py tests/test_account_session_recovery.py && git commit -m "feat: add account re-login and automatic recovery"`

### Task 6: Regression, Security, and Scale Contracts

**Files:**
- Modify: `tests/test_account_session_recovery.py`
- Modify: `tests/test_personal_browser_lifecycle.py`
- Modify: `docs/USER_GUIDE_ZH.md`
- Modify: `docs/FORK_DIFFERENCES_ZH.md`

**Interfaces:**
- No new production interface; this task proves unchanged boundaries and documents the one-time migration behavior.

- [ ] **Step 1: Add scale and adversarial tests**

  Cover 0/1/200/500 account candidate scans, 200 stored profiles with zero startup browser processes, profile traversal/symlink attacks, corrupted/missing profiles, disabled-account exclusion, and sanitized API/log/error output. Prove the live process ceiling with deterministic 10/11 concurrency plus launch failure, launch timeout, cancellation, close exception, and mixed ordinary personal-pool + reauth/recovery occupancy; the 11th instance must not enter the real browser-start boundary until a live lease is released.

- [ ] **Step 2: Run focused adversarial tests**

  Run: `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest tests/test_account_session_recovery.py tests/test_personal_browser_lifecycle.py -q`

- [ ] **Step 3: Update user-facing documentation**

  Explain in plain Chinese that existing accounts need one re-login after this upgrade, future same-machine restarts reuse profiles, another machine needs its own login, browser processes start only when needed and close afterward, and profile data must be backed up with `flow.db` only while the service is stopped.

- [ ] **Step 4: Run complete automated gates**

  Run:

  `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m pytest -q`

  `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo\venv\Scripts\python.exe -m compileall -q src`

  `git diff --check`

  Require all tests to pass and no compilation/diff errors.

- [ ] **Step 5: Run credential/runtime scanners**

  Scan tracked files and the candidate delivery tree for private keys, bearer/API-key values, Cookie/ST/AT material, profile directories, runtime databases, media, cache, logs, and browser state. Require zero findings after allowlisting documentation placeholders.

- [ ] **Step 6: Commit docs and regression coverage**

  Run: `git add tests/test_account_session_recovery.py tests/test_personal_browser_lifecycle.py docs/USER_GUIDE_ZH.md docs/FORK_DIFFERENCES_ZH.md && git commit -m "test: verify durable account recovery boundaries"`

### Task 7: Controlled Local Cutover and Real Acceptance

**Files:**
- Create locally only: `runtime-validation/account-session-recovery-acceptance.json`
- Update after success: `docs/FINAL_VALIDATION_ZH.md`

**Interfaces:**
- Acceptance report may contain only stage/status/counts/error class/process counts; no identity, credentials, profile path, URL, prompt, media, or response body.

- [ ] **Step 1: Back up the stopped runtime state**

  Stop the current service cleanly, copy `flow.db` and `data/account_profiles` to a timestamped local backup, record file counts and SHA-256 without reading credential content, then start the candidate from this worktree.

- [ ] **Step 2: Re-login one account interactively**

  User handles Google login/passkey/CAPTCHA. Verify the service stores one opaque profile reference, marks the account `ok`, closes the onboarding browser, and does not reveal credentials or paths.

- [ ] **Step 3: Verify service and Windows restart recovery**

  Restart the service, then perform the user-approved Windows restart. Verify the account remains recoverable without re-import and no browser starts merely because the service starts.

- [ ] **Step 4: Verify controlled expiry recovery and final idle zero**

  Use a synthetic expiry fixture or non-secret test hook first. Confirm one recovery browser starts, refresh succeeds, and process count returns to zero after idle TTL. Do not force a paid generation retry loop.

- [ ] **Step 5: Run one low-cost image and one Omni Flash video**

  Submit each once only after account state is `ok`. Record only terminal status, media-present boolean, duration class, service-alive boolean, and final browser process count.

- [ ] **Step 6: Re-run all gates on the exact candidate**

  Repeat full pytest, compileall, diff-check, and scanners. Any failure keeps the branch RED and blocks packaging/push.

### Task 8: Clean Delivery, Private Repository, and Rollback Evidence

**Files:**
- Update: `docs/FINAL_VALIDATION_ZH.md`
- Update: `docs/MILESTONE_REPORT.md`
- Create outside repository: clean ZIP and `.sha256`

**Interfaces:**
- Delivery ZIP excludes `.git`, virtual environments, caches, build output, databases, runtime reports, logs, media, browser state, account profiles, and all credentials.

- [ ] **Step 1: Review dirty scope before cleanup**

  Use `git status --short --branch`, `git diff --stat`, and ignored-file preview. Delete only confirmed generated runtime artifacts inside the delivery copy; never recursively delete a computed broad path.

- [ ] **Step 2: Author-separated review**

  Review data migration, state transitions, identity binding, profile containment, browser cleanup, UI non-disclosure, and unchanged scheduler behavior against the design spec. Fix findings with focused RED/GREEN cycles.

- [ ] **Step 3: Update final evidence**

  Record exact commit, automated test counts, real-acceptance status, known limits, backup/rollback location class, and difference from upstream without secrets.

- [ ] **Step 4: Push only after user-authorized acceptance**

  Push the non-force branch/commit to the existing private repository after all Task 7 gates pass. Do not deploy or create a public repository.

- [ ] **Step 5: Build and verify clean ZIP**

  Build from the committed tree, scan the archive manifest and extracted copy, compute SHA-256, and verify the archive commit matches the pushed private repository.

## Plan Self-Review

- Spec coverage: state model, no auth-driven disable, candidate scan, single flight, backoff, profile isolation, onboarding/re-login, automatic recovery, identity mismatch, lifecycle zero, UI/API privacy, scale, migration, rollback, real image/video, clean delivery, and private push each map to a task.
- Placeholder scan: no unfinished or deferred implementation placeholders are present.
- Type consistency: the five Token fields, three Database/Profile interfaces, browser constructor extension, and admin endpoint names are consistent across tasks.
