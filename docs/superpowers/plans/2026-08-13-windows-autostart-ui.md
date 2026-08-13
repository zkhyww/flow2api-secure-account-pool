# Windows Autostart UI Implementation Plan

> **For agentic workers:** Implement inline in the current dirty checkout. Do not commit, push, open PRs/issues, deploy, create/delete a real Windows scheduled task, or run real Flow generation.

**Goal:** Add an admin-session-protected Windows-login autostart toggle backed by the real fixed Task Scheduler entry, plus a safe one-click local startup script, without adding database state.

**Architecture:** Put all Task Scheduler interaction behind a small `src/services/windows_autostart.py` service whose repository paths and task name are server-owned constants. The admin API accepts only a boolean and delegates to that service. The management page renders only the returned real status, while `start-flow2api.pyw` independently provides idempotent manual startup/open behavior without a console window.

**Tech Stack:** Python 3.11, FastAPI, unittest/pytest, PowerShell Task Scheduler cmdlets behind a mocked subprocess boundary, static HTML/JavaScript, Windows CMD.

## Global Constraints

- Preserve the entire existing dirty worktree and touch only files required by this batch.
- Fixed task name: `Flow2API-Local-Account-Pool`.
- Fixed action: current repository `venv\Scripts\python.exe` + `main.py` + current repository working directory.
- No client-controlled task name, command, path, PowerShell, or executable arguments.
- State enum is exactly `enabled | disabled | error | unsupported`.
- Non-Windows returns `unsupported` without Task Scheduler calls.
- No account DB persistence for this switch.
- All management endpoints require the existing admin session dependency.
- Tests mock the Windows system boundary; no real task changes in this batch.
- No real Flow generation, service restart, account enable/disable, commit/push/PR/issue/deploy.

---

### Task 1: Close the existing unattended-soak test-isolation defect

**Files:**
- Modify: `tests/test_real_unattended_soak_contract.py`
- Verify: `scripts/real_unattended_soak.py`

**Interfaces:**
- Consumes: existing `atomic_write_report(Path, dict)`.
- Produces: isolated contract test using `tempfile.TemporaryDirectory`.

- [x] Reproduce the current `FileExistsError` caused by the fixed repository-root directory.
- [x] Replace the fixed directory fixture with a system temporary directory without deleting the existing residue.
- [x] Run `./venv/Scripts/python.exe -m pytest tests/test_real_unattended_soak_contract.py -q` and require all existing cases/subtests to pass.

### Task 2: Write Windows-autostart RED contracts

**Files:**
- Create: `tests/test_windows_autostart_contract.py`
- Modify: `tests/test_batch5_manage_contract.py` only if a shared static contract is clearer there.

**Interfaces:**
- Future production module: `src.services.windows_autostart.WindowsAutostartManager`.
- Future admin endpoints: `GET/POST /api/admin/windows-autostart`.
- Future static controls: `cfgWindowsAutostart`, `windowsAutostartStatus`, `windowsAutostartReason`, `loadWindowsAutostart`, `setWindowsAutostart`.

- [x] Add a fake command runner that records invocations and supplies deterministic Task Scheduler query/register/unregister results.
- [x] Assert non-Windows returns `unsupported` and invokes no runner.
- [x] Assert missing/mismatched/disabled tasks return `disabled`, exact enabled template returns `enabled`, and query failure returns `error`.
- [x] Assert enable uses only the fixed task/action/working-directory template and repairs mismatches; assert disable removes only the fixed task.
- [x] Assert enable/disable are idempotent and mutation failure re-renders the previous/fresh real state with a reason.
- [x] Build a tiny FastAPI app with `admin.router`; verify both endpoints return 401 without an active admin session and succeed with one.
- [x] Assert the management page contains the approved Chinese copy and renders checkbox state from the GET/POST response rather than persisting a local/DB value.
- [x] Assert `start-flow2api.pyw` behaviorally deduplicates when `/health` is ready, uses only fixed repository venv + `main.py` argv/cwd with `shell=False`, waits for readiness, opens `/manage`, and reports failure via a GUI message.
- [x] Run the new focused test and confirm it fails because the production service/endpoints/UI/script do not yet exist.

### Task 3: Implement the fixed Windows Task Scheduler service

**Files:**
- Create: `src/services/windows_autostart.py`

**Interfaces:**
- `WindowsAutostartManager.get_status() -> dict[str, object]` with `status` and `reason`.
- `WindowsAutostartManager.set_enabled(enabled: bool) -> dict[str, object]`.
- Module-level accessor `get_windows_autostart_manager()` for API wiring/test patching.

- [x] Derive repository root from the module location and set immutable task/action constants.
- [x] Implement a default subprocess runner that invokes `powershell.exe` only with internal fixed scripts and never logs stdout/stderr.
- [x] Implement fixed query JSON projection for task existence, enabled state, action executable/argument/working directory, trigger type/user, StartWhenAvailable and RestartCount.
- [x] Normalize Windows paths case-insensitively and classify exact template as `enabled`; all other readable states as `disabled`.
- [x] Implement idempotent enable with `Register-ScheduledTask -Force` using current-user logon, limited run level, StartWhenAvailable, RestartCount 3, one-minute RestartInterval.
- [x] Implement idempotent disable with `Unregister-ScheduledTask` for the one fixed task name only.
- [x] On mutation failure, read state again and return that state with a bounded stable reason; never return raw stdout/stderr.
- [x] Run service-focused tests to GREEN.

### Task 4: Add admin-session API wiring

**Files:**
- Modify: `src/api/admin.py`

**Interfaces:**
- `WindowsAutostartRequest(enabled: bool)`.
- `GET /api/admin/windows-autostart`.
- `POST /api/admin/windows-autostart`.
- Both use `Depends(verify_admin_token)`.

- [x] Add the boolean-only request model and service import/accessor.
- [x] Add GET/POST endpoints using `asyncio.to_thread` around the synchronous OS boundary.
- [x] Do not add any public-route alias or database write.
- [x] Run API auth/status focused tests to GREEN.

### Task 5: Add the management-page real-state toggle

**Files:**
- Modify: `static/manage.html`

**Interfaces:**
- Controls: `cfgWindowsAutostart`, `windowsAutostartStatus`, `windowsAutostartReason`.
- Functions: `renderWindowsAutostart`, `loadWindowsAutostart`, `setWindowsAutostart`.

- [x] Add the approved label “Windows 登录后自动启动 Flow2API” and concise explanation that Task Scheduler is the source of truth.
- [x] GET state during config loading; render enabled/disabled/error/unsupported deterministically.
- [x] POST only `{enabled: checkbox.checked}` on change; immediately re-render from the response; on request exception call GET again so optimistic UI state is not retained.
- [x] Disable the checkbox for unsupported/error until a later successful refresh.
- [x] Run static management contracts and full-page JavaScript parse to GREEN.

### Task 6: Add the one-click local `.pyw` launcher

**Files:**
- Create: `start-flow2api.pyw`

**Interfaces:**
- Local port: `127.0.0.1:8000`.
- Health endpoint: `http://127.0.0.1:8000/health`.
- Management page: `http://127.0.0.1:8000/manage`.

- [x] Resolve the repository root only from `Path(__file__).resolve().parent`; do not accept a path argument.
- [x] If fixed localhost `/health` is already ready, open `/manage` and exit without launching Python.
- [x] Otherwise verify `venv\Scripts\python.exe` and `main.py`, launch exactly one fixed argv with repository cwd and `shell=False`, and poll `/health` for at most 60 seconds.
- [x] On readiness open `/manage`; on missing files, launch failure, or health timeout show a concise non-secret `tkinter.messagebox` error.
- [x] Run behavior contracts covering deduplication, exact argv/cwd, readiness wait, and failure notification to GREEN; do not execute the launcher against the real service in this batch.
- [x] Treat the partial `start-flow2api.cmd` left by the blocked earlier approach as non-deliverable; current connector has no delete operation, so Codex must remove that newly added file in the clean handoff.

### Task 7: QC reports and gates

**Files:**
- Create/update: `docs/superpowers/reports/2026-08-13-real-unattended-soak-harness-qc.md`
- Create: `docs/superpowers/reports/2026-08-13-windows-autostart-ui-qc.md`

- [x] Run new focused tests and related management/static contracts.
- [x] Run full `pytest`.
- [x] Run `compileall` without modifying source semantics.
- [x] Parse the complete JavaScript from `static/manage.html` with the repository-available JS runtime/tooling.
- [x] Run credential-pattern scan and report counts only, never matching text.
- [x] Run `git diff --check`.
- [x] Record exact RED→GREEN evidence, touched files, and explicit non-actions: no real Flow generation and no real Task Scheduler toggle.
