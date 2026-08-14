# Flow2API Local Secure Account Pool — Milestone Report

Updated: 2026-08-14

## Conversation provenance

- Current active write lane: https://chatgpt.com/c/6a7e4b8f-b5a8-83ee-99b5-8135b71d8b56. This ordinary ChatGPT conversation is the only writable web coding lane for the current closure and is frozen after handoff.
- Superseded evidence only (read-only; never write back):
  - https://chatgpt.com/c/6a7e4337-d72c-83ee-9eab-20381fbeb35e
  - https://chatgpt.com/c/6a7e1a84-81dc-83ee-baae-5f6644cc5936
  - https://chatgpt.com/c/6a7e17a0-6a04-83ee-aef5-3c07d051698f
  - https://chatgpt.com/c/6a7e0d32-f800-83e8-a53e-0645363d609c
  - https://chatgpt.com/c/6a7e0c09-175c-83e8-92e3-2a4a31ac9bb2

## Milestone 0 — Fresh takeover baseline

- Workspace: `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo`
- Branch: `main`
- HEAD: `2b9856088d47b2d29f77bc077e2340482d67e734`
- Opening dirty scope: 59 porcelain entries; preserved in place, no reset/clean/checkout.
- Fresh focused baseline: `tests/test_yingce_adapter_contract.py tests/test_yingce_file_cache_privacy.py tests/test_compat_video_tasks.py` => 21 passed + 5 subtests passed.
- First interpreter probe (`python`) was unavailable in the shell; repository interpreter `venv/Scripts/python.exe` is the verified runner.
- Security review A–E remains the current blocker; no production fix has been applied at this milestone.
- Privacy boundary remains in force: no credentials, account identities, raw prompts/media, response bodies, database/browser state, or complete upstream/media URLs are to be read or reported.

## Milestone 1 — Yingce resource/lifecycle security closure

- Upload limits are connected to image edits and video reference uploads; over-limit uploads fail before generation with stable `media_too_large` errors.
- Oversized inline generated image media is mapped to a stable 502 `media_too_large` response instead of escaping the endpoint.
- Video background work now runs through a guarded task wrapper, converts ordinary failures into stable failed registry state, consumes task exceptions, and supports bounded shutdown of current-loop background tasks.
- Application lifespan shutdown awaits the Yingce background-task shutdown helper from `finally`.
- Remote media download keeps the exact host allowlist, standard-port enforcement, per-hop redirect validation, and DNS pinning while switching to bounded streaming with a configurable default limit of 64 MiB (`FLOW2API_REMOTE_MEDIA_MAX_BYTES`). Oversize streaming aborts before cache publication and leaves no partial media file.
- Fixture-only follow-ups strengthened the streaming tests without relaxing production checks: synthetic sessions now deliver content through `content_callback`, and the lifecycle fixture rejects any fallback read of buffered `response.content`.
- Fresh resource lifecycle: `tests.test_yingce_resource_lifecycle_security` => 6/6 passed.
- Fresh Yingce security/privacy/compat/resource aggregate: `unittest discover -s tests -p 'test_yingce*.py'` => 29/29 passed.
- Fresh full suite: `unittest discover -s tests` => 347/347 passed in 89.459s.
- Privacy boundary remains in force: no credentials, account identities, raw prompts/media, response bodies, database/browser state, or complete upstream/media URLs were read or reported during this closure.
- Final static closure: `git diff --check` passed (line-ending warnings only); branch remains `main` at `2b9856088d47b2d29f77bc077e2340482d67e734`; current porcelain count is 54 and the dirty worktree remains preserved in place.
- Status: READY_FOR_CONTROLLER_REVIEW. No reset/clean/commit/stage/push/PR/deploy was performed.

## Milestone 2 — Adversarial security follow-up

- Fresh review found and fixed two deterministic ASGI multipart edge cases: a post-body disconnect was previously replaced with a synthetic empty request, and a disconnect before body completion still invoked downstream. Both were RED first and now preserve disconnect semantics without duplicate response or hang.
- Fresh review also found the TTL same-update race in `CompatVideoTaskRegistry.update`: cleanup could mark an active task `failed/task_timeout` and the same call could immediately overwrite it as completed. The RED failed with actual `completed`; the minimal GREEN makes terminal states immutable to late updates.
- The forged-low `Content-Length`, chunked actual-byte limit, multiple-reference total limit, mask fail-closed, T2V reference fail-closed, TLS verification, per-hop redirect validation, download size cap, and DNS RESOLVE contracts remain covered.

## Milestone 3 — Proxy pinning and Windows autostart UI P1

- Security reviewer follow-up established that `CurlOpt.RESOLVE` and curl `CONNECT_TO` are not sufficient proof that an HTTP proxy CONNECT is pinned to the prechecked origin IP. Explicit HTTP media proxies now use the dedicated pinned HTTP CONNECT helper: CONNECT authority is the prechecked public IPv4/IPv6 address, TLS `server_hostname` remains the origin host, the tunneled GET `Host` remains the origin host, and proxy authentication is confined to CONNECT. Non-HTTP proxy schemes fail closed before DNS/session work.
- The explicit proxy path no longer uses `AsyncSession`; blocking proxy socket/TLS work runs through `asyncio.to_thread`. No-proxy secure downloads still require allowlisted hosts, standard ports, global DNS answers, `verify=True`, manual redirect handling, per-hop RESOLVE pinning, and `trust_env=False` so environment proxies cannot bypass the pinned path.
- Added contracts cover IPv4/IPv6 CONNECT authority, CONNECT failure fail-closed behavior, proxy-auth non-leakage, Content-Length/chunked/close response framing, forged-low response length actual-byte enforcement, per-hop redirect revalidation, TLS origin hostname verification, and the production absence of `CONNECT_TO`.
- Controller-provided real UI evidence found a Windows autostart false-negative caused only by qualified-versus-bare rendering of the same current-user identity. The deterministic RED uses synthetic identities only. The minimal rule accepts exact matches or the one-way current-qualified/observed-bare same-leaf form; different user leaves and different qualified prefixes remain rejected.
- Fresh focused Windows suite: `13 passed, 11 subtests passed`.
- Fresh security + autostart aggregate: `62 passed, 22 subtests passed in 1.74s`.
- Fresh full pytest after both P1 fixes: `364 passed, 107 subtests passed in 100.71s`.

## Evidence classification and remaining acceptance

- **Automatic/simulated evidence completed here:** unit/ASGI/security contracts, mock Windows system boundary, fake download/proxy boundary, full pytest. No real network media proxy was contacted by the new proxy test.
- **Older real evidence retained, not repeated here:** previously documented controlled Flow pressure, video batch, idle recycle/re-wake, and Windows reboot/login-state recovery.
- **Controller-provided current real evidence:** the fixed Windows autostart task was reported enabled while the UI showed disabled because of identity display normalization. This coding conversation did not inspect the real username or task XML and did not toggle the real task.
- **Still pending Codex real acceptance:** refresh/recheck the management-page autostart status with the fixed identity matcher; long unattended soak remains separate. ZIP/private-GitHub delivery, deployment and Zeabur are not claimed complete.
- Current real API follow-up found that safe proxy rejection made a provider-returned official image URL collapse to `media_empty` because the adapter still tried to force a server-side b64 download. Deterministic RED: `2 failed, 4 subtests passed`; minimal GREEN: allowlisted HTTPS/standard-port/no-userinfo image URLs return OpenAI Images `{url: ...}` without calling FileCache, while untrusted/http/userinfo/nonstandard-port candidates remain `media_empty` and are not reflected. Focused image RED→GREEN finished at `2 passed, 4 subtests passed`; security/adapter/resource aggregate then passed `50 passed, 15 subtests passed`, and full pytest passed `365 passed, 111 subtests passed in 100.88s`.
- No real Flow, service restart, system-task mutation, commit, push or deploy was performed by this coding conversation.
- Final four-core rerun was split into two tool-safe batches and totals `25 passed, 6 subtests passed`; `compileall -q src tests` passed with no output; strict secret-format scan count was `0`; `git diff --check` passed with only existing LF/CRLF conversion warnings.

## Milestone 4 — Pinned HTTP media proxy and delivery documentation

- Fresh local evidence confirms curl `RESOLVE`/`CONNECT_TO` cannot be used as proof that an HTTP proxy CONNECT target is pinned. The negative capability test is retained and uses only a local synthetic proxy.
- Explicit HTTP media proxies use a dedicated fixed-IP CONNECT transport: each hop first passes allowlist/HTTPS/standard-port/userinfo/global-DNS checks; CONNECT targets the prechecked public IPv4/IPv6 address; TLS and tunneled `Host` remain the original official hostname; proxy authentication is confined to CONNECT; redirects are revalidated per hop; response bytes are bounded; blocking socket/TLS work runs through `asyncio.to_thread`.
- Unsupported proxy schemes fail closed rather than falling back to proxy-side origin DNS. No-proxy media downloads keep the RESOLVE + TLS verification path.
- Yingce image compatibility keeps safe official HTTPS URL passthrough for allowlisted URLs while data/local results stay `b64_json`. Yingce video compatibility materializes remote media to a local filename before `/content`; complete upstream media URLs are not stored in the task registry, and compatibility video task IDs remain process-local.
- README now links `docs/USER_GUIDE_ZH.md` and `docs/FORK_DIFFERENCES_ZH.md` near the top while preserving the upstream README body and attribution. Delivery examples use `<your_api_key>`; the docs privacy contract rejects legacy placeholders/example secret formats and URL-key guidance.
- Automatic evidence before final gate: pinned-proxy security + Yingce adapter focused `40 passed, 23 subtests passed`; broader Yingce security/resource/compat aggregate `65 passed, 23 subtests passed`; delivery-doc privacy contract `3 passed`. One local curl capability probe emits a Windows event-loop compatibility warning but is not a test failure.
- This coding lane did not run real Flow, inspect a real proxy value, inspect a current API key, mutate the real Windows scheduled task, restart the service, commit, push, deploy, or perform Zeabur work. Real Yingce video smoke, Windows autostart UI recheck, long unattended soak, ZIP/private-GitHub handoff and any deployment remain controller acceptance items unless separately evidenced by Codex.
- Final automatic gate after documentation cleanup: full pytest `384 passed, 119 subtests passed` with one non-failing local curl capability warning; `compileall -q src tests` passed; delivery-doc secret-format count `0`; legacy placeholder/literal count `0`; `git diff --check` passed with only existing LF/CRLF conversion warnings.
- Active conversation URL is unavailable to the DevSpace/tool runtime; no browser URL is fabricated or copied from superseded evidence.

## Milestone 5 — Personal stale orphan cleanup after restart

- Fact correction: normal personal idle TTL is healthy. Controller generation-only evidence showed the current-run increase returned from 34 processes to the same 21-process baseline after the configured 600-second TTL plus a reaper cycle. The remaining baseline was three older roots with six children each; all roots predated the current service and their parents no longer existed. No idle/TokenManager/token-pool/TTL production logic was changed.
- Root cause: `BrowserCaptchaService.cleanup_stale_runtime_artifacts()` existed but only cleaned old runtime files and was not called from application startup, so prior-service project browser groups could remain after restart.
- RED: startup cleanup was awaited zero times; there was no exact repo-profile/dead-parent selector; cleanup did not route a selected stale root through the existing PID-tree reaper. Tests used synthetic process records and temporary directories only.
- Minimal GREEN: startup now runs stale cleanup before creating the personal browser service. Ownership requires exact `--user-data-dir` parsing, a known profile under this checkout, exclusion of active current-runtime paths, and a dead parent. Incidental command-line substring matches are not accepted as ownership. Existing PID-tree cleanup is reused; no new lifecycle manager, timer, global browser sweep, or TTL change was added.
- Automatic coverage includes old repo-owned orphan, live owner, active current runtime, external browser, multiple old worker roots, lazy startup, and later on-demand runtime creation.
- Final gates: browser lifecycle/pool `48 passed`; Yingce `83 passed, 84 subtests passed`; full pytest `414 passed, 180 subtests passed`; `compileall -q src tests scripts main.py` passed. One unrelated existing 0.5-second Batch 3 timing test transiently failed on an earlier full run, then passed five consecutive isolated reruns and the subsequent full suite, so no unrelated change was made.
- Unverified risk: this lane did not restart the service or touch real browser processes, so the observed pre-restart orphan baseline still needs Codex real acceptance. Metadata lookup failures fail closed by preserving the process rather than risking an unrelated browser.
- Codex real revalidation: record only project-attributed root/child counts, parent-liveness, generation timestamps and idle seconds; perform a controlled service restart; verify pre-restart project roots are gone while unrelated Chrome/Edge counts are unchanged; verify startup creates no new personal root; run one controlled task, wait more than 600 seconds plus a reaper cycle and verify its current-generation runtime is reclaimed; run one more task and verify on-demand recreation; confirm accounts remain active without plugin pairing or re-login. Do not expose command lines, profile contents, credentials, URLs, prompts, media, or response bodies.
- No reset/clean/checkout/commit/push/deploy, service restart, Windows restart, real Flow request, database-content read, or credential read was performed in this milestone.

## Milestone 6 — Final code-side closure audit

### Fresh-read baseline and ownership

- Fresh workspace read confirmed `D:\\CodexWorkspaces\\Flow2API-Secure-Account-Pool\\repo`, branch `main`, HEAD `2b9856088d47b2d29f77bc077e2340482d67e734`, with the pre-existing aggregate dirty worktree preserved in place. No reset, clean, checkout, stage, commit, push or deployment action was used. Final porcelain count is 65 entries; the aggregate candidate remains intentionally dirty for Codex review.
- No repository `AGENTS.md`, `CLAUDE.md` or `pyproject.toml` exists in this checkout. `requirements.txt` remains the production/runtime dependency manifest; the clean-environment follow-up adds a separate `requirements-test.txt` for test-only dependencies. README, the Chinese user guide, the local account-pool plan/validation documents, this milestone report, the Windows-autostart design, the Yingce adapter design, and the candidate implementation/test files were fresh-read before closure decisions.
- The original architecture boundaries remain intact: existing Token/TokenManager, LoadBalancer, ConcurrencyManager, personal browser pool, FlowClient, GenerationHandler and FileCache stay authoritative. This milestone added no second account store, browser manager, lifecycle manager, queue, credential store or upstream client.

### Reproducible REDs and minimal fixes

1. **Malformed quoted `--user-data-dir` ownership RED.** A command-line token shaped as `--user-data-dir="<managed-path>"junk` was incorrectly accepted by the candidate parser because the regex stopped at the closing quote without requiring a token boundary. RED: parser contract failed. GREEN: `_extract_command_line_switch_value()` now requires whitespace/end-of-command after the parsed value while retaining quoted/unquoted and case-insensitive switch forms.
2. **Missing parent metadata RED.** A synthetic managed Chrome record with missing `ParentProcessId` was converted to parent PID 0 and selected as stale. RED expected preservation but got the process PID. GREEN: stale selection now requires a positive parent PID; missing/invalid metadata fails closed and preserves the browser.
3. **Process-enumeration failure RED.** A synthetic metadata-scan exception escaped `cleanup_stale_runtime_artifacts()` and could block startup cleanup. GREEN: that scan boundary now returns no kill candidates and logs only the exception class; no process tree is touched on uncertain metadata.
4. **Whole-cleanup startup failure RED.** Even after the scan boundary was hardened, `src/main.py` directly awaited stale cleanup; a synthetic cleanup exception aborted the application lifespan before the personal service could initialize. RED reproduced at the lifespan boundary. GREEN: startup keeps cleanup before `BrowserCaptchaService.get_instance(db)`, but wraps the cleanup as best-effort and records only the exception class before continuing.
5. **Clean-delivery scanner RED.** Delivery docs and ignore rules existed, but there was no reusable count-only scanner for a prepared clean copy. RED failed because `scripts/scan_delivery_secrets.py` was absent. GREEN: the new scanner rejects known runtime/secret paths before opening their contents, scans only text candidates, counts secret-format findings without printing matched values, and exits non-zero when forbidden paths or secret patterns are found. Its unit contract uses only synthetic temporary content.
6. **Clean-delivery false-positive RED.** Codex independently reproduced `forbidden_path_count=6` and `secret_pattern_count=17` on the live working tree. Count-only file classification showed one match in the unchanged upstream-baseline `src/services/flow_client.py` and sixteen synthetic authorization-header fixtures under tests. The upstream public value is identical between HEAD and the current file. RED: a temporary clean source tree containing that one exact upstream value at its exact relative path plus an explicit synthetic test fixture still reported two secret findings. Minimal GREEN: the scanner exempts the upstream value only by exact relative path plus an exact SHA-256 fingerprint; the same value at any other path and a one-character value change at the allowed path still count as findings. Under `tests/`, only a matched value whose match text itself carries an explicit `fixture`, `synthetic`, `placeholder` or `test-` marker is classified as synthetic; the tests directory is not skipped. Three older extension-status fixtures that lacked such semantics were rewritten as split source expressions using an explicit fixture session marker rather than adding another whitelist. Unknown Google-key-shaped, GitHub-token-shaped, Bearer and private-key-header fixtures placed under `tests/` still produce four findings. Scanner output remains limited to `files_scanned`, `forbidden_path_count` and `secret_pattern_count`; neither paths nor matched values are printed.
7. **Clean-environment test dependency RED.** Codex independently created a fresh clean-delivery environment and proved that installing only `requirements.txt` leaves pytest unavailable; adding pytest alone then fails test collection because Pillow is unavailable; adding Pillow makes the complete suite pass. RED contract in this lane requires an independent `requirements-test.txt`, exact package coverage for pytest and Pillow, and continued absence of both packages from production `requirements.txt`. Minimal GREEN: add unpinned `pytest` and `Pillow` to the separate test manifest only, document that runtime/service installation uses `requirements.txt` alone, and document that development/CI/clean-delivery full validation additionally installs `requirements-test.txt`. No production dependency was changed.

### Adversarial orphan-cleanup review

- Windows enumeration is limited to Chrome/Chromium/Edge process names and uses CIM metadata for PID, parent PID, name and command line. Ownership is not inferred from incidental substrings: the browser must expose an exact `--user-data-dir` switch whose normalized path equals this checkout's default profile or a directly managed runtime-profile child under this checkout.
- Profile comparison is case-normalized and path-normalized; active current-runtime profile paths are excluded. The current service PID is excluded, and any browser whose parent PID is still live is preserved. This also makes parent-PID reuse conservative: if an unrelated process has reused the old parent PID, cleanup defers rather than risking a kill.
- Pool workers contribute their active runtime paths before cleanup selection. A browser owned by another live worker/service process is therefore also protected by the live-parent rule. Multiple stale roots can still be handed independently to the existing PID-tree reaper; no global Chrome/Edge sweep was introduced.
- Missing/invalid parent metadata, parent-liveness probe errors and enumeration failures all fail closed. Startup cleanup remains before personal-service construction, while a cleanup exception can no longer prevent service startup.
- Ordinary Chrome/Edge profiles, profiles from another checkout, and command lines that merely mention this checkout's path are not ownership evidence. No real process enumeration, termination or service restart was performed by this coding lane.

### Planned-gap audit: code status

- **Windows autostart UI and one-click launch:** code is present and backed by the real Task Scheduler state rather than a database boolean. The fixed task template, qualified/bare current-user identity rule, UI re-render from server state, unsupported/error handling, launcher health deduplication, single fixed spawn, ready polling and bounded error messaging are covered. Fresh focused evidence before the final aggregate gate: `14 passed, 11 subtests passed`.
- **Test page API key handling:** the default authenticated management path issues an in-memory test capability and uses same-origin test endpoints; the page contains no `localStorage` or `sessionStorage` use. The optional external API-key field remains password-type and memory-only. No new persistence path was needed.
- **Public image/video catalog:** the shared catalog remains six capabilities: two image capabilities plus Omni Flash and three Veo 3.1 video capabilities. Omni Flash exposes validated 8/10 seconds with 10 seconds default; Veo 3.1 public capabilities remain 8 seconds. Legacy compatible IDs stay callable but are not advertised as current menu entries.
- **Yingce async video compatibility:** create/poll/content, bounded process-local registry, strong background-task references, TTL cancellation/final-state protection, local media content delivery and `Idempotency-Key` digest/fingerprint behavior remain in place. The original `/v1/models` and `/v1/chat/completions` routes remain covered by compatibility regression tests; no second provider path was added.
- **Account pool/lifecycle:** existing dense-pack scheduling, configured concurrency, max-10 browser protection, lazy browser startup, affinity, restart recovery, 0/1/200/500 synthetic scale and pagination remain covered. No `200` account hard cap was introduced.
- **Delivery boundary:** `.gitignore` already excludes runtime databases/logs/env/config/profile/cache paths needed for a clean handoff; README/user guide/fork-difference docs use approved placeholders and privacy guidance. Production/runtime installation remains `requirements.txt` only; full development/CI/clean-delivery validation additionally installs `requirements-test.txt`, which contains only pytest and Pillow and does not modify the production manifest. The count-only scanner distinguishes the one exact upstream public constant and explicit synthetic test fixtures from unknown credential-shaped values without weakening the runtime-path gate. On the live working tree it reports `secret_pattern_count=0` while still reporting the six expected runtime forbidden paths and exiting non-zero; a prepared clean copy must remove those runtime paths and reach both counts at zero. ZIP/SHA-256/private-repository creation and push remain explicitly outside this coding lane.
- No additional production code was changed for these planned areas because fresh tests did not produce a reproducible RED.

### Final serial automatic gates

- Added/related focused set: `93 passed, 77 subtests passed`.
- Browser lifecycle/pool set: `65 passed`.
- All Yingce tests: `83 passed, 84 subtests passed`; the known `curl_cffi` Windows Proactor compatibility warning is non-failing.
- First final full-suite run: one unrelated existing 0.5-second Batch 3 synthetic concurrency wait timed out; all other tests passed (`419 passed, 190 subtests passed`). The failing test was then rerun unchanged five consecutive times and passed 5/5. No unrelated production or assertion change was made.
- Subsequent full suite: `420 passed, 190 subtests passed` with only the same non-failing `curl_cffi` warning.
- `venv/Scripts/python.exe -m compileall -q src tests scripts main.py`: PASS with no output.
- `git diff --check`: PASS; only existing LF/CRLF conversion warnings were emitted.
- Clean-delivery follow-up focused gate: `tests/test_local_delivery_docs_contract.py tests/test_extension_connection_status.py` => `11 passed, 10 subtests passed`.
- Live-tree count-only scanner after the false-positive fix: `forbidden_path_count=6`, `secret_pattern_count=0`; the non-zero exit is intentionally preserved because runtime artifacts are still present in this working tree.
- Clean-delivery follow-up full suite: `423 passed, 190 subtests passed` with only the same non-failing `curl_cffi` warning.
- Clean-delivery follow-up `venv/Scripts/python.exe -m compileall -q src tests scripts main.py`: PASS with no output.
- Clean-delivery follow-up `git diff --check`: PASS; only existing LF/CRLF conversion warnings were emitted.
- Clean-environment dependency focused gate: `tests/test_test_dependency_manifest_contract.py tests/test_local_delivery_docs_contract.py` => `10 passed, 10 subtests passed`.
- Clean-environment dependency full suite: `425 passed, 190 subtests passed` with only the same non-failing `curl_cffi` warning.
- Clean-environment dependency `venv/Scripts/python.exe -m compileall -q src tests scripts main.py`: PASS with no output.
- Clean-environment dependency `git diff --check`: PASS; only existing LF/CRLF conversion warnings were emitted.
- Live-tree scanner after the dependency-manifest/docs update: `files_scanned=151`, `forbidden_path_count=6`, `secret_pattern_count=0`; the non-zero exit remains intentional until Codex prepares the clean copy.

### Code present but still requiring Codex real acceptance

- Real post-restart stale-orphan removal: verify the pre-restart project-attributed roots are removed while unrelated Chrome/Edge processes are unchanged. This coding lane did not terminate any real process.
- Reconfirm normal personal runtime behavior after that restart: startup should create no browser root, one controlled task should start on demand, the current-generation runtime should reclaim after more than 600 seconds plus a reaper cycle, and a later task should recreate on demand without account re-login.
- Recheck the real Windows autostart management UI against the actual scheduled-task state, and independently verify the double-click launcher keeps one listener and reports failure cleanly. This lane did not modify or inspect the real task.
- Re-run the existing reliable real video/unattended harness only under the controller's approved real-test policy; do not place API keys or credentials on the command line or in reports.
- Prepare a separate clean delivery copy; do not copy runtime `data`, `tmp`, browser profile data, local settings, databases, logs, HAR files, env files or other ignored runtime artifacts. Run the clean-copy automated gates, then run `venv/Scripts/python.exe scripts/scan_delivery_secrets.py <clean-copy-path>` and require both `forbidden_path_count=0` and `secret_pattern_count=0`. Only after that may Codex produce the ZIP, SHA-256 and private GitHub handoff.

### Codex next steps

1. On the current candidate, independently run `venv/Scripts/python.exe -m pytest -q`, `venv/Scripts/python.exe -m compileall -q src tests scripts main.py`, and `git diff --check` without altering the dirty worktree.
2. Perform the controlled stale-orphan restart acceptance using count-only process evidence: project-attributed root/child counts, parent liveness, generation timestamps and idle seconds only. Do not capture or publish command lines, profile contents, credentials, prompts, media, response bodies or upstream/media URLs.
3. Perform the real autostart-UI/launcher check and the approved on-demand generation/idle-reclaim sequence. Stop on any unexpected real behavior rather than retrying paid generation blindly.
4. Build a clean copy with the runtime/secret exclusions above. In its fresh Python environment install `requirements.txt` for runtime dependencies, then additionally install `requirements-test.txt` before running the full validation suite; require the count-only delivery scanner to return zero findings before packaging.
5. Only after clean-copy acceptance: create the ZIP and SHA-256, create/use the intended **private** GitHub repository, and push the clean source according to the controller's delivery procedure. None of those external delivery actions were performed here.

### Unverified risks / conservative behavior

- Parent PID reuse is deliberately safety-biased: a stale browser whose old parent PID has been reused by a live unrelated process can be retained as a false negative until a later cleanup opportunity. This is preferable to killing a browser on ambiguous ownership and should be observed in the real restart acceptance.
- The stale cleanup is invoked on startup when the effective CAPTCHA mode is `personal`; this lane did not establish a RED requiring a global cleanup sweep for other CAPTCHA modes, so it did not broaden process ownership or startup behavior beyond the current personal path.
- Real Windows Task Scheduler identity/rendering, actual service restart ordering, real browser-process TOCTOU, long unattended behavior, clean-copy packaging and remote private-repository state cannot be proven by unit tests in this lane and remain controller acceptance items.
- The current active browser conversation URL is now recorded explicitly from controller-provided browser evidence in the provenance section; no superseded URL is substituted for it.

### Stop-write boundary

- Source/test/document writes for Milestone 6 ended with that milestone update. No real service/Windows restart, real account operation, real Flow image/video request, real process cleanup, database-content read, credential read, packaging, commit, push, repository creation or deployment was performed.

## Milestone 7 — Final pre-handoff evidence audit

### Fresh-read scope and conversation provenance

- Fresh workspace evidence remains `D:\\CodexWorkspaces\\Flow2API-Secure-Account-Pool\\repo`, branch `main`, HEAD `2b9856088d47b2d29f77bc077e2340482d67e734`. Opening porcelain count was 65 entries and the aggregate dirty candidate was preserved in place; no reset, clean, checkout, stage, commit or push was used.
- The current ordinary ChatGPT conversation recorded in the provenance section is the active write lane for this final audit. The former lane named “代码审计与修复” is superseded evidence only and must not write this workspace.
- Fresh read confirmed that the Yingce image create and video create/poll/content endpoints all retain `verify_api_key_flexible` HTTP authentication and reuse the same application `GenerationHandler`; video execution additionally reuses the existing compatibility registry and media materialization path. No second generation client or auth-bypass production route was added.
- Fresh read also reconfirmed `requirements-test.txt` contains only unpinned `pytest` and `Pillow`, production `requirements.txt` remains separate, README and the Chinese guide describe the runtime/test dependency split, and the count-only scanner still exposes only `files_scanned`, `forbidden_path_count` and `secret_pattern_count`.

### Reproducible RED and minimal GREEN

- **Missing Yingce image real-smoke entry:** the existing safe acceptance harness already covered video `POST /v1/videos` → poll → `/content`, but it had no equivalent command for `POST /v1/images/generations`. RED first: `tests/test_real_unattended_soak_contract.py` failed with `2 failed, 11 passed, 7 subtests passed` because `--kind image` was not a valid CLI choice and `run_yingce_image_smoke` did not exist.
- Minimal GREEN changed only the existing acceptance harness and its contract. `--kind image` now makes one loopback image-create call using the existing environment-only acceptance credential and existing HTTP auth; it deliberately does not retry a transport-failed image create. The output allowlist is limited to `stage`, `status`, `error_class`, `has_media`, `duration_seconds` and `create_http`, so media data/URL, task identifiers, credentials and prompts are not emitted. The existing video smoke behavior remains create-with-idempotency → poll → content.
- Focused RED→GREEN for the harness: `13 passed, 7 subtests passed`. The Chinese guide now records the two controller commands and the privacy/retry boundary. No Yingce production endpoint, auth dependency, `GenerationHandler`, account pool, browser lifecycle or model catalog code changed for this RED.

### Adversarial no-RED review

- The expanded focused audit covered Yingce adapter/security/resource lifecycle, stale-orphan ownership and startup cleanup failure, personal browser lifecycle/process cap/idle wake-up, 0/1/200/500 synthetic stress, account-model availability, canonical/test-page model catalog, Windows autostart, clean-delivery docs/scanner and test-dependency manifest. Result: `176 passed, 120 subtests passed` with one known non-failing `curl_cffi` Windows Proactor warning.
- Browser stale selection remains conservative: ordinary Chrome/Edge profiles are not repo ownership, missing/non-positive parent metadata is preserved, live-parent or parent-liveness probe uncertainty is preserved, and process-enumeration/cleanup exceptions cannot block application startup. No real browser process was inspected or terminated here.
- Browser pool tests continue to enforce the 10-process cap, queue excess demand, preserve available-worker concurrency, reclaim idle runtime and recreate on demand; the synthetic scale contracts continue to cover 0/1/200/500 accounts. No reproducible browser/concurrency RED was found.
- Account-specific model availability remains a separate verified fact from the six-capability catalog, and the test page labels `available`/`unavailable` explicitly while keeping hidden diagnostic mappings out of the normal public selection flow. No reproducible API/UI availability-mixing RED was found, so no UI/catalog code was changed.

### Final serial automatic gates

- Focused audit: `176 passed, 120 subtests passed`; one existing non-failing `curl_cffi` Windows Proactor warning.
- Full pytest: `427 passed, 190 subtests passed`; the same warning only. The two-test increase from the previous 425 baseline is exactly the new Yingce image-smoke CLI/behavior contract.
- `venv/Scripts/python.exe -m compileall -q src tests scripts main.py`: PASS with no output. The first equivalent invocation was blocked by the DevSpace safety layer before execution; the unchanged validation target was immediately rerun with `./`-qualified paths and passed.
- `git diff --check`: PASS; only existing LF/CRLF conversion warnings were emitted.
- Live-tree scanner: `files_scanned=151`, `forbidden_path_count=6`, `secret_pattern_count=0`. Exit code remains intentionally non-zero because runtime forbidden paths exist in the live checkout; the secret count is zero. A prepared clean delivery copy must still reach `0/0` before packaging.

### Files changed by Milestone 7

- `scripts/real_unattended_soak.py` — add one-shot Yingce image compatibility smoke and `--kind image` CLI entry while retaining environment-only auth and strict result fields.
- `tests/test_real_unattended_soak_contract.py` — add the two RED→GREEN image-smoke contracts.
- `docs/USER_GUIDE_ZH.md` — document controller-only image/video smoke invocation and privacy/retry boundaries.
- `docs/MILESTONE_REPORT.md` — record this final audit, evidence and handoff boundary.

### Still unverified / Codex-controlled acceptance

- This lane did not send a real image or video generation, read a real credential/database/configuration, restart the service or Windows, inspect/kill real Chrome/Edge, mutate Task Scheduler, package a clean copy, compute delivery SHA-256, create a repository, commit or push.
- Codex still needs real Yingce compatibility acceptance against the running candidate: first image create, then video create/poll/content, while retaining the normal HTTP auth contract. No auth dependency override is required for this path because the acceptance harness already accepts the existing credential through its environment-only interface.
- Codex still needs controlled stale-orphan restart acceptance, real autostart/launcher recheck, normal on-demand browser → idle reclaim → re-wake evidence, and the clean-copy `forbidden_path_count=0` / `secret_pattern_count=0` gate before packaging.

### Codex next executable command

- With the approved acceptance credential already present in the existing environment-only slot, run: `venv\Scripts\python.exe scripts\real_unattended_soak.py --kind image`.
- If that controlled image smoke completes with `has_media=true`, the next compatibility command is `venv\Scripts\python.exe scripts\real_unattended_soak.py --kind video`; do not retry paid work blindly after an unexpected result.

### Final stop-write boundary

- After the post-report verification below, this ordinary ChatGPT lane stops writing. The aggregate dirty candidate remains for Codex review and real acceptance; all delivery synchronization, packaging, SHA-256, commit, push and final conclusion remain Codex-owned.

## Milestone 8 — Four-hour unattended soak terminal takeover

- Fresh DevSpace read at takeover: `stage=running`, `status=running`, `planned_count=16`, `completed_count=1`, `failed_count=0`, `image_count=1`, `video_count=0`, `has_media_count=1`, `service_alive=true`, `browser_final_zero=false`, `browser_process_count=0`, `started_at=2026-08-14T00:35:35.750778Z`.
- Single-run evidence after excluding the querying PowerShell process: one `run_unattended.ps1` runner and one `real-unattended-soak*.json` report are active. The two matching `temp_adapter_host.py` Python processes are both descendants of that one runner and include one parent/child pair, so this is one host process tree rather than a duplicate soak.
- Git baseline remains unchanged for delivery gating: development tree `main` is still at `2b9856088d47b2d29f77bc077e2340482d67e734` with the aggregate dirty candidate preserved; `final-delivery` is clean on `main` at `1660fe76fc5124e0db8c242331c1337655abd91d`.
- The current active ordinary ChatGPT conversation URL is `https://chatgpt.com/c/6a7e65cb-7240-83ea-ac62-b36370ccca21`; it is the sole terminal-closure write lane. The prior evidence chats `https://chatgpt.com/c/6a7e6460-0df4-83ee-a05a-3935103d0fea` and `https://chatgpt.com/c/6a7e5ad5-bd08-83e9-aa3d-9f1c275333a7` are both superseded read-only evidence.
- While the soak remains running, do not restart it, send another real task, change source code, commit, push or rebuild the delivery archive.
- Next terminal check must require all of: `stage=finished`, `status=completed`, `failed_count=0`, `completed_count=planned_count=16`, `has_media_count=16`, `service_alive=true`, `browser_final_zero=true`, and `browser_process_count=0`. Only then may final validation documentation, isolated full tests, ignored-runtime cleanup, scanner, commit/push, ZIP/SHA rebuild and completion audit begin.

## Milestone 9 — Four-hour unattended soak terminal completion

- Fresh DevSpace terminal read confirms the completion gate is fully satisfied: `stage=finished`, `status=completed`, `planned_count=16`, `completed_count=16`, `failed_count=0`, `image_count=12`, `video_count=4`, `has_media_count=16`, `service_alive=true`, `browser_final_zero=true`, `browser_process_count=0`, `finished_at=2026-08-14T04:33:22.149624Z`.
- The previous approximately four-hour run remains historical failure evidence: 16 planned, 15 successful with media, 1 `transport_error`, and `service_alive=false` at the end. It is retained for provenance but is superseded by the new 16/16 all-green terminal result and is not the current conclusion.
- The final, 16th real video round completed through another available account slot after the controller temporarily disabled the preferred slot for that acceptance check. The temporary adjustment was then restored, and all 5 slots were enabled at completion.
- No account identity is recorded here. This terminal documentation also does not record Cookie, Token, API Key, full Flow URL, prompt, media, or response body.
- Latest full automated gate baseline for the terminal record is `427 passed` plus `190 subtests passed`; the older 425 count is obsolete.
