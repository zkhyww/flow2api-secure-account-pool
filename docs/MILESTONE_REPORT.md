# Flow2API Local Secure Account Pool — Milestone Report

Updated: 2026-08-16

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

## Milestone 10 — Durable account session recovery

> Author-review follow-up: the later Milestone 11 supersedes the Task 6 conclusions below that treated a fresh empty re-login candidate as the final contract and treated the launch-parallelism gate as proof of the maximum-10 live-browser limit. The Task 6 counts remain historical evidence for that earlier candidate.

### Scope and plan/source corrections

- This milestone is limited to durable account authentication recovery. The existing account pool, dense-pack behavior, learned concurrency, global browser cap, circuit breaker, media parsing and ordinary generation worker lifecycle remain the existing owners.
- The implementation plan named a credential persistence test file that is not present in this checkout. The existing credential authority is `tests/test_batch4_credential_persistence.py`, so focused compatibility checks use that file instead of creating a parallel duplicate.
- The personal browser service already owns the global browser launch gate. Persistent authentication recovery reuses that gate; no second semaphore or browser-count implementation was added.
- The browser capture boundary returns private session state but not an authoritative account identity. Identity binding therefore stays at the existing ST-to-AT conversion boundary, which yields the account email used for fail-closed comparison before credentials are committed.
- Explicit re-login uses a fresh candidate Profile instead of reopening the target account's old Profile. This is intentionally stricter than the initial plan: a user could otherwise sign the old Profile into a different account before identity verification. The candidate becomes the stored reference only after identity match, leaving the previous account state untouched on mismatch.
- Protocol and personal-browser ST refreshes were also corrected to treat a new ST as an in-memory candidate. ST/AT are committed only after ST-to-AT identity verification; this closes the same overwrite window in background and foreground refresh paths without changing generation scheduling.

### RED-to-GREEN evidence

- Data model/migration RED proved the five authentication recovery fields and database APIs were absent; focused GREEN was `4 passed`.
- Profile-store RED was an absent module; containment/restart/symlink contracts reached `5 passed, 9 subtests passed`.
- Authentication-state RED reproduced permanent disable and manual-disable reversal; state-machine and existing credential/failure-routing checks reached `28 passed` in the focused combination.
- Persistent-browser RED reproduced missing durable Profile support; browser and pluginless onboarding checks reached `30 passed`.
- Recovery/onboarding/re-login/API/UI RED groups separately reproduced missing persistent recovery, missing Profile binding, missing re-login route, unstable public auth status and missing UI action. The Task 5 focused aggregate reached `66 passed, 49 subtests passed`.
- Task 6 fresh baseline first confirmed the pre-existing recovery suite was `28 passed`, then exposed gaps not covered by that suite. The added deterministic REDs reproduced three account-refresh failures (`3 failed, 27 passed`): a background protocol ST candidate was written when ST-to-AT raised, current-AT validation logged raw exception text, and protocol-refresher shutdown logged raw exception text. Personal-browser REDs separately reproduced raw refresh/pool exception logging (`2 failed, 24 passed`); onboarding RED reproduced a runtime exception type escaping as public `error_class` (`1 failed, 9 passed`); the persistent-Profile-log/manual-refresh-API combination reproduced four privacy failures (`4 failed, 26 passed, 6 subtests passed`).
- Task 6 minimal GREEN keeps protocol and personal ST values in memory until ST-to-AT identity verification, removes the background exception-path ST write, keeps persistent Profile paths redacted in lifecycle logs, maps onboarding failures to stable public status, and removes raw exception text from the authentication refresh logs/API paths covered by the REDs. The focused identity/privacy aggregate is `70 passed, 6 subtests passed`; scale plus existing browser-cap/credential/admin/UI regressions are `69 passed, 56 subtests passed`.

### Durable recovery behavior

- Each migrated account stores only an opaque Profile key in the database; the local Profile directory remains below the ignored `data/account_profiles/` root and is never returned by the account API or management UI.
- `is_active` remains user intent. Authentication failures move through `refresh_pending`, bounded `backoff` or `reauth_required` metadata and do not call the permanent-disable path.
- Automatic recovery follows existing protocol refresh first, then the account's persistent Profile when needed. A recovery browser is on-demand, reuses the existing global launch gate and is closed on success, failure and timeout.
- Ordinary generation workers keep their temporary Profile behavior. Durable Profiles are authentication recovery sources only.
- The public management surface is restricted to five stable states: 正常、等待自动恢复、稍后重试、需要重新登录、已停用. Internal Profile identifiers, paths and raw authentication errors are not public fields.
- Upgraded pre-existing accounts need one explicit re-login each to establish a durable Profile. Same-machine, same-Windows-user restarts can then recover from that local state; another machine requires separate normal logins rather than Profile copying.

### Task 6 final automatic gates

- Exact-candidate full pytest: `484 passed, 209 subtests passed`; repeated final-state runs kept the same counts, with one existing non-failing `curl_cffi` Windows Proactor compatibility warning and zero test failures or errors.
- `venv/Scripts/python.exe -m compileall -q src tests scripts main.py`: PASS with no output.
- `git diff --check`: PASS with no output.
- Count-only live-tree privacy scan: `files_scanned=159`, `forbidden_path_count=4`, `secret_pattern_count=0`. The scanner intentionally exits non-zero on the live worktree because runtime-only paths are present; they were not opened or removed. Git tracking checks report `profile_tracked_count=0`, `runtime_sensitive_tracked_count=0`, and the persistent Profile root is ignored.
- `docs/USER_GUIDE_ZH.md` now states that same-machine durable recovery backup requires the stopped-service pair `data/flow.db` plus the complete `data/account_profiles/` root; this pair is not a cross-machine migration format and must not enter Git, delivery ZIPs, logs or public reports. `docs/FORK_DIFFERENCES_ZH.md` records the identity-before-commit and stable-error privacy boundaries.
- No commit, push, deploy, service restart, real database read, real browser login, real account operation or paid generation was performed by this Task 6 lane.

### Unverified real-world acceptance

- No real account, credential, Profile content, live database, running service, Google login, CAPTCHA, Passkey, two-step verification or paid generation was used in this milestone.
- Controller/Codex acceptance still needs one normal existing-account re-login, a same-machine service restart recovery, an expired-session automatic recovery, a forced transient refresh failure that remains retryable, an identity-mismatch fail-closed check using controlled accounts, and confirmation that all opened recovery browsers close afterward while the global browser count remains bounded.

## Milestone 11 — Re-login Profile reuse and live browser-process cap review

### Fresh review findings

- Fresh source review confirmed the design required explicit re-login to reuse the target account's existing Profile state, while the Task 6 implementation always created a new empty candidate and its API test encoded that drift as a contract.
- Fresh source review also confirmed the existing global semaphore covered only the `initialize()` startup region. After successful initialization it was released even though the browser remained alive; the ordinary pool separately capped configured workers at 10, but direct onboarding/re-login/recovery instances could exist outside that worker list. The earlier scale test proved only configuration clamping, not a mixed live-process ceiling.

### RED-to-GREEN evidence

- Profile/re-login RED: `6 failed, 6 passed, 9 subtests passed`. The failures proved the store lacked safe clone/remove operations, successful re-login did not inherit the previous Profile state, and failed/mismatched re-login left candidate directories behind.
- Live-process RED: `6 failed, 4 passed, 4 subtests passed`. Deterministic synthetic browser starts proved an 11th instance could enter the real browser-start boundary while 10 instances were still live. The same gap reproduced after start failure, start timeout, cancellation, a shutdown-cleanup exception, and in a mixed ordinary personal-pool plus recovery-instance scenario.
- Minimal Profile GREEN adds contained clone/remove operations. Explicit re-login clones an existing Profile to an isolated candidate, opens only that candidate, retains the existing ST-to-AT identity-before-commit boundary, switches the stored Profile reference only on identity match, removes the superseded Profile after success, and removes the candidate on failure. An account with no usable existing Profile starts from an empty candidate.
- A follow-up post-write RED then exposed one remaining fail-closed gap: the old implementation wrote candidate credentials/Profile reference before separate auth-success/enable calls, so a later exception could leave the database pointing at a candidate that the outer failure path deleted. The direct reauth RED was `1 failed, 4 passed`; after removing duplicate inherited DB fixtures, the focused atomicity RED was `3 failed, 34 passed`.
- Minimal atomic GREEN adds `Database.commit_account_reauth()`: verified credentials, Profile reference, auth-success metadata, explicit enable state and consecutive-error reset are committed in one SQLite transaction. A failure during a later statement explicitly rolls the transaction back, so the old Profile reference remains authoritative and the candidate can be safely cleaned. The ST-to-AT identity check still occurs before this transaction, and there is no asynchronous post-commit state step. Atomic focused GREEN is `45 passed`.
- Cleanup durability RED then reproduced a temporary filesystem-lock failure: `1 failed, 8 passed, 9 subtests passed`. Minimal GREEN writes a Profile-root cleanup marker containing only the opaque key before deletion; a blocked delete stays tracked and is retried on the next candidate creation, while Store construction alone performs no cleanup. The Profile-store GREEN is `9 passed, 9 subtests passed`, and the widened account/profile/browser/docs regression is `114 passed, 29 subtests passed`.
- Minimal browser GREEN keeps the existing startup-parallelism semaphore unchanged and adds one fixed maximum-10 live-process lease shared by every `BrowserCaptchaService` instance. The lease is acquired before entering browser startup, retained after successful initialization, released by shutdown `finally`, and also released after launch failure, timeout, cancellation, or shutdown-cleanup failure. The ordinary pool scheduler, dense-pack behavior, learned concurrency, circuit breaker and worker semantics are unchanged.
- Earlier GREEN checkpoints remain useful regression evidence: first combined Profile/live-cap GREEN was `22 passed, 13 subtests passed`; the earlier wider account/profile/browser recovery regression was `100 passed, 19 subtests passed`; tightened live-cap plus documentation contracts were `20 passed, 14 subtests passed`.

### Final automatic gates for the reviewed candidate

- Full pytest after the atomic reauth and cleanup-durability follow-ups: `497 passed, 209 subtests passed` with the existing non-failing Windows `curl_cffi` compatibility warning only. The increase from the fresh 484 baseline is exactly thirteen new regression tests: nine Profile/live-cap contracts, three post-write/transaction atomicity contracts, and one durable cleanup-marker contract.
- `compileall -q src tests scripts main.py`: PASS with no output.
- `git diff --check`: PASS with no output.
- Count-only live-tree scanner: `files_scanned=159`, `forbidden_path_count=4`, `secret_pattern_count=0`. The non-zero scanner exit remains intentional for the live working tree because runtime-only forbidden paths still exist; their contents were not opened or removed.

### Remaining real acceptance

- No real account, credential, Profile content, browser login, running service, database content or paid generation was used in this review. Real acceptance still needs a controlled existing-account re-login that proves prior login state is actually reused, identity mismatch remains fail-closed, same-machine restart/expiry recovery works, and the process count never exceeds 10 while ordinary workers and recovery browsers overlap. No blind paid retry is permitted.

## Milestone 12 — Onboarding candidate cleanup and atomic Profile binding

### Independent QC finding and RED

- Codex review found that the native onboarding launcher created a persistent candidate Profile before browser capture but only closed the browser in `finally`. A capture exception or persistence exception therefore left an unreferenced Profile directory behind.
- Two filesystem-backed regression tests reproduced the gap: capture failure and persistence failure both left the candidate directory present (`2 failed, 10 deselected`).
- The same review found a related ordering risk for both new and existing-account imports. A Profile key could be written before all remaining persistence steps completed, while the launcher treated any raised exception as permission to delete the candidate.

### Minimal GREEN

- The launcher now retains a candidate only after persistence returns successfully. Browser close runs first in the cleanup path; every unsuccessful candidate is then passed to the existing contained Profile-store cleanup boundary, including its durable opaque-key retry marker.
- A new-account import now finishes the existing token/project creation workflow without a Profile reference and binds the Profile key only after that workflow returns successfully. A partial add can therefore no longer leave the database pointing at a candidate removed by the launcher.
- An existing-account import now writes credentials, Profile reference and authentication-success metadata in one database update and performs no asynchronous post-write authentication step. The account's existing enabled/disabled intent remains unchanged.
- Focused onboarding and persistence regression: `20 passed`. Fresh exact-candidate full pytest after all source and test edits: `499 passed, 209 subtests passed`, with the same single non-failing Windows `curl_cffi` compatibility warning.
- No service switch, real database/Profile read, real login, account operation, commit, push, deployment or paid generation was performed by this QC closure.

## Milestone 13 — Long-running re-login browser launch recovery

### Fresh root-cause investigation

- The long-running personal-browser pool is configured for the existing maximum of 10 workers. The live-process cap is a class-level semaphore whose lease is intentionally retained after a successful browser start and normally released by managed shutdown.
- A deterministic lifecycle gap existed after an already-started worker browser died asynchronously. `shutdown_idle_runtime_if_needed()` returned immediately when the nodriver browser reported `stopped` or disconnected, before the existing managed-shutdown `finally` could release the still-held live-process lease. The per-worker idle reaper continued calling that method, so repeated dead workers could accumulate logical "live" leases even when no Chrome process remained and eventually prevent a new re-login browser from reaching `nodriver.start`.
- The explicit re-login path also destroyed the evidence needed to diagnose a historical failure. `capture_account_onboarding_result()` includes browser initialization, opening the onboarding window and session capture, but `_run_account_reauth()` mapped every non-timeout exception from that whole call to `browser_start_failed`. A later `close()` exception could additionally replace an earlier interactive timeout. Therefore the exact exception behind the already-recorded historical `browser_start_failed` cannot be reconstructed from the stored state alone.
- Safe environment checks found no configured personal/request proxy value and no Windows desktop-session split for the running service. No proxy value, account value, credential, Profile content or raw response was read or emitted.

### RED-to-GREEN evidence

- The final deterministic RED was `3 failed, 15 passed, 4 subtests passed`. The three failures proved: a stopped browser kept its live-process lease after idle cleanup; a post-start capture exception was still stored as `browser_start_failed`; and a close exception overrode an earlier interactive timeout.
- Minimal lease GREEN re-checks a held lease under the existing browser lifecycle lock. If the browser is still absent, stopped or marked disconnected after that lock is obtained, it runs the existing managed shutdown path, which releases the existing global lease in its established `finally`. If initialization is merely still in progress, the lock wait and re-check prevent a premature release. No second semaphore, retry loop or process-count mechanism was added.
- Minimal re-login GREEN makes browser initialization an explicit first stage. Only that stage maps to `browser_start_failed`; a later non-timeout onboarding/capture exception remains transient and maps to the existing `network` backoff class, while `TimeoutError` remains `interactive_verification`. Cleanup runs afterward, and a close failure is recorded as a secondary failure without replacing an already-established primary result.
- Re-login diagnostics log only `stage`, stable `error_class` and `exception_type`. Raw exception text is never added by this boundary, so the diagnostic record cannot expose Cookie/Token/API key, Profile path, account identity, proxy, full URL or raw upstream response.
- RED target after GREEN: `18 passed, 4 subtests passed`. Wider account-recovery/browser-lifecycle/onboarding/Profile regression: `117 passed, 13 subtests passed`.

### Final automatic gates

- Fresh exact-candidate full pytest: `502 passed, 209 subtests passed`, with the existing single non-failing Windows `curl_cffi` Proactor compatibility warning. The increase from Milestone 12 is exactly the three new regression contracts above.
- `python -m compileall -q src tests scripts main.py`: PASS.
- `git diff --check`: PASS; Git emitted only existing working-copy LF-to-CRLF conversion warnings.
- Count-only live-tree scanner: `files_scanned=159`, `forbidden_path_count=5`, `secret_pattern_count=0`. The scanner still exits non-zero by design for forbidden runtime paths in the live worktree. The count is one higher than Milestone 12's recorded live-tree count; those runtime objects were not opened, deleted or modified by this repair lane.
- Files changed by this milestone are limited to `src/api/admin.py`, `src/services/browser_captcha_personal.py`, `tests/test_account_reauth_api.py`, `tests/test_account_session_recovery_scale.py`, and this report. Existing dirty candidate files outside these hunks were preserved.
- No commit, push, package, deployment, service restart, real browser login, real account retry, Profile-content inspection or paid generation was performed.

### Remaining controlled real validation

- A controller should perform one deliberate existing-account re-login against the long-running candidate service. The acceptance signal is that the dedicated headed browser reaches the login window; if it still fails, the new value-free diagnostic must identify whether the failure stage is `initialize`, `capture` or `close` and report only the exception type.
- A controlled long-running lifecycle check should also allow a personal worker browser to terminate, wait for the existing idle reaper, and confirm a later re-login can acquire the shared live-process capacity without blind retries. The global maximum must remain 10 throughout.

### Controller real-runtime acceptance — 2026-08-17

- Codex independently repeated the final candidate gates before switching the local service: focused re-login/lifecycle tests were `18 passed, 4 subtests passed`; full pytest was `502 passed, 209 subtests passed`; compileall and `git diff --check` passed; the count-only live-tree scan remained `files_scanned=159`, `forbidden_path_count=5`, `secret_pattern_count=0`.
- The running port-8000 service was replaced with this worktree while preserving the existing data junction. The replacement service returned HTTP 200 from `/health`.
- Safe aggregate inspection found five pre-upgrade accounts and no established persistent account Profile. Four accounts were temporarily enabled one at a time for protocol refresh checks and were restored to their original disabled state afterward; the previously enabled acceptance account remained enabled. No account identity or credential value was recorded.
- Every protocol refresh check failed, so no account could recover unattended from the pre-upgrade credentials. A real image request was then attempted once and returned no media because no authenticated account slot was available. No blind retry and no real video request followed.
- One deliberate explicit re-login did reach a dedicated visible Chrome window: total Chrome process/window counts changed from `48/1` before the call to `57/2` while the call was active. The call later returned a retryable result and the database stored `auth_state=backoff`, `last_auth_error_class=network`, and no Profile; it did not regress to `browser_start_failed`.
- Cleanup acceptance passed: after the failed re-login/refresh checks, total Chrome counts returned to `48/1`, the count of Chrome processes whose command line belonged to Flow2API runtime/Profile paths was `0`, and the service remained healthy. Counts include unrelated user Chrome processes; only the Flow2API-owned count is the cleanup authority.
- Full same-machine restart recovery, real image generation, real Omni Flash video generation, ordinary generation-worker idle-TTL wake/reclaim, and Windows shutdown remain blocked on one normal manual Google login per pre-upgrade account. The machine was intentionally not shut down because the user's real-generation acceptance condition was not met.

## Milestone 14 — Re-login refresh-clock and healthy-AT recovery guard

### Fresh source verification

- New controller evidence reported that all five accounts had completed explicit re-login and had persistent Profile, ST, AT and Google Cookies, yet three accounts were changed back to `reauth_required` within minutes with stable `interactive_verification` / `protocol_refresh_failed` metadata while their AT expiry remained comfortably beyond the one-hour refresh boundary. This repair lane did not read the live database, credential values, Profile contents, account identities or proxy values.
- Fresh source review confirmed that `Database.commit_account_reauth()` atomically wrote verified credentials, Profile reference and `auth_state='ok'` but did not update `last_st_refresh_at` or `last_st_refresh_result`. `run_protocol_refresh_once()` runs from the existing 60-second loop and treats a missing/old `last_st_refresh_at` as due under the existing per-account/default refresh interval.
- Fresh source review also confirmed that `_refresh_protocol_token()` unconditionally invoked `_try_persistent_profile_recovery()` whenever protocol ST refresh returned no candidate. A 45-second Profile capture timeout then marks `interactive_verification` with `interactive=True`, which promotes the account to `reauth_required` even when the currently stored AT is still clearly usable for more than one hour.
- The management page refresh path is read-only with respect to authentication refresh: `DOMContentLoaded` calls `refreshTokens()`, which loads `/api/tokens` and stats. `/refresh-at` is invoked only by the explicit AT-refresh action, so an ordinary page reload was not the state transition source.

### RED-to-GREEN evidence

- Four focused contracts were added before production changes. RED was `3 failed, 1 passed, 3 subtests passed`: reauth did not refresh the ST-success clock, an immediate protocol-refresh tick therefore called `_refresh_protocol_token`, and a failed protocol ST refresh drove a healthy two-hour AT into interactive Profile recovery. The preservation contract already passed for AT missing, expiry unknown and expiry below one hour, proving the existing fail-closed recovery chain for genuinely at-risk AT state.
- Minimal transaction GREEN adds `last_st_refresh_at = CURRENT_TIMESTAMP` and `last_st_refresh_result = 'success'` to the existing `commit_account_reauth()` UPDATE. These values now commit or roll back with the verified ST/AT, Profile reference, auth-success state and explicit enable state; no post-commit refresh marker write was added.
- Minimal protocol-refresh GREEN re-reads the token after a failed protocol ST attempt and reuses the existing `_should_refresh_at()` authority. If the fresh token still has an AT with at least the existing one-hour safety margin, the background refresher preserves credentials and auth state and leaves the stable `protocol_refresh_failed` result for diagnostics. If AT is absent, expiry is unknown, or remaining lifetime is below one hour, the existing persistent-Profile recovery path still runs unchanged and remains fail-closed.
- The four new contracts are GREEN at `4 passed, 3 subtests passed`. The widened reauth/auth-state/Profile/onboarding recovery set is `67 passed, 18 subtests passed`.

### Automatic gates

- Fresh full pytest after the production/test changes: `506 passed, 212 subtests passed`, with the same single non-failing Windows `curl_cffi` Proactor compatibility warning. The increase from the 502/209 baseline is exactly four new tests and three subtests from this milestone.
- `python -m compileall -q src tests scripts main.py`: PASS.
- `git diff --check`: PASS; only existing working-copy LF-to-CRLF warnings were emitted.
- Count-only live-tree scanner: `files_scanned=159`, `forbidden_path_count=5`, `secret_pattern_count=0`. The scanner still exits non-zero for the known live runtime paths; their contents were not read, changed or removed.
- This milestone changes only the relevant hunks in `src/core/database.py`, `src/services/token_manager.py`, `tests/test_account_session_recovery.py`, plus this report. Existing dirty and untracked candidate material is preserved.
- No live `flow.db` read or mutation, credential/Profile inspection, port-8000 restart/switch, real generation, commit, push or deployment was performed by this lane.

### Remaining controlled real validation

- Codex still needs to switch/restart the service only when explicitly authorized so the running process loads this candidate, then verify that recently re-logged accounts remain `正常` across several 60-second protocol-refresher ticks and ordinary management-page reloads.
- Controlled acceptance should also force or observe one protocol ST refresh failure while the current AT has more than one hour remaining and confirm the account stays usable without opening an interactive recovery browser; a separate near-expiry/unknown-expiry case should confirm the existing Profile recovery path still activates.
- Real image/video generation remains a separate acceptance gate and must not be used as a blind retry mechanism for authentication recovery.

## Milestone 15 — Healthy-AT validation repairs historical auth-state false positives

### Fresh source verification and controller evidence boundary

- Controller acceptance after Milestone 14 reported three accounts still carrying historical `reauth_required` / `interactive_verification` metadata created by the old code even though their stored AT remained beyond the one-hour safety boundary. Two were returned to `ok` through the existing explicit AT-refresh path. A final account entered the full recovery chain and timed out, so blind retries and direct database edits were explicitly prohibited. This repair lane did not read or mutate the live database, account values, credentials or Profile contents.
- Fresh source review confirmed the remaining same-root boundary in `TokenManager.ensure_valid_token()`: when `_should_refresh_at()` is false, a successful upstream `get_credits` proves the current AT is usable, but the success branch previously updated only credits plus the in-memory AT validation cache and returned without clearing historical auth failure metadata.
- Fresh call-chain review confirmed the existing admin `refresh-credits` action already calls `TokenManager.refresh_credits()`, which calls production `ensure_valid_token()` before the balance refresh. No endpoint, button or background task is needed for this repair.

### RED-to-GREEN evidence

- The focused RED was `1 failed, 2 passed, 35 deselected`. The failing contract proved that a token with AT lifetime above one hour, historical `reauth_required` / `interactive_verification`, and `last_st_refresh_result='protocol_refresh_failed'` remained falsely marked after a successful production `ensure_valid_token()` / `get_credits` validation. The two passing protection contracts proved a failed upstream validation followed by failed refresh does not fake auth success, and an already-clean `ok` account does not need an auth-success rewrite.
- Minimal GREEN changes only the successful upstream-validation branch. After credits are persisted, it re-reads the token and calls the existing `_mark_auth_success()` only when the fresh auth metadata is not clean `ok` (`auth_state`, failure count, retry deadline or stable auth error class). A clean account performs no extra auth-state write.
- `_mark_auth_success()` intentionally does not touch ST-refresh history, so `last_st_refresh_result='protocol_refresh_failed'` remains available as historical diagnostics rather than being forged to `success`.
- Focused GREEN is `3 passed, 35 deselected`; widened account-session/auth-status/reauth regression is `48 passed, 9 subtests passed`.

### Automatic gates

- Fresh full pytest after the production/test changes: `508 passed, 212 subtests passed`, with the existing single non-failing Windows `curl_cffi` Proactor compatibility warning. The increase from the Milestone 14 baseline is exactly two net new tests because one prior validation-failure test was expanded into three contracts.
- `python -m compileall -q src tests scripts main.py`: PASS.
- `git diff --check`: PASS; only the existing working-copy LF-to-CRLF warnings were emitted.
- Count-only live-tree scanner remains `files_scanned=159`, `forbidden_path_count=5`, `secret_pattern_count=0`; the non-zero exit is still due to known live runtime paths whose contents were not read, changed or removed.
- This milestone changes only the relevant hunk in `src/services/token_manager.py`, the focused contracts in `tests/test_account_session_recovery.py`, and this report. Existing dirty/untracked candidate material is preserved.
- No service restart/switch, live account operation, real API call, live database mutation, Profile inspection, generation, commit, push or deployment was performed by this lane.

### Controlled recovery for the final historical false positive

- After an authorized service switch loads this candidate, Codex can make exactly one controlled use of the existing `refresh-credits` action for the remaining historical false-positive account. Because `refresh_credits()` enters `ensure_valid_token()` first, a successful current-AT `get_credits` validation will clear only the stale auth failure metadata to `ok` and return a usable token while preserving `last_st_refresh_result='protocol_refresh_failed'`.
- If that upstream validation fails, the new code does not mark the account `ok`; the existing refresh/recovery path retains authority and a failed chain remains failed. Do not repeat the action blindly and do not edit the database manually.

### Controller real-runtime acceptance — final candidate

- Codex independently verified the final working tree at `508 passed, 212 subtests passed`; `compileall` PASS; `git diff --check` PASS; count-only scanner `files_scanned=159`, `forbidden_path_count=5`, `secret_pattern_count=0`.
- The service was controllably restarted from the unique worktree and loaded the final candidate; `/health` returned HTTP 200.
- Three accounts still carried historical `reauth_required` state created by the old code. The existing explicit AT-refresh path returned two to `ok`. The third entered the full forced refresh/recovery chain, failed, and correctly remained non-`ok` rather than being falsely marked healthy.
- After the healthy-AT self-repair patch was loaded, Codex performed exactly one existing `refresh-credits` / `ensure_valid_token` validation for that remaining account. Its upstream AT validation and subsequent recovery chain both failed, so it correctly remained `reauth_required`; this now represents a genuine need for interactive re-login and no further blind retry was performed.
- The other four accounts remained `正常` across two complete 60-second background refresh periods and two ordinary management-page reloads. The prior symptom where multiple accounts fell back to re-login after a page refresh did not recur.
- After recovery attempts completed, the Flow2API-owned Chrome process count returned to 0 and the service still returned HTTP 200 from `/health`.
- Codex removed only the zero-byte untracked `0)` file created by its own failed diagnostic escaping. No other runtime or dirty file was deleted; the count-only scanner remained `159/5/0`.
- No paid generation, commit, push or deployment was performed in this acceptance. Four accounts are currently usable; the remaining account requires one real interactive login through the existing “重新登录并启用” flow.

## Milestone 16 — 影策视频能力拆分与真实生成验收（2026-08-17）

### 网页工程 lane

- 当前 active URL：`https://chatgpt.com/c/6a8314b9-17e0-83e9-b49b-c1d0d6b28e86`（普通聊天，GPT-5.6 Sol 极高 + DevSpace办公）。
- 旧 URL：`https://chatgpt.com/c/6a830f8b-ec50-83ee-a302-e6e0652c9e40`，仅作为 superseded evidence 保留，不再写入。

### 本轮实现

- 公开目录把 Omni Flash 与 Veo 3.1 的文生、首帧、首尾帧和 References 拆成独立入口；公开能力 ID 不再依靠图片数量猜测生成方式。
- 旧内部模型 ID 继续兼容；真实上游模型 ID 仍只存在于兼容映射，不暴露给调用方。
- 显式入口严格校验参考图数量。720P/native/空值沿用上游原生清晰度；不支持的 1080P、4K、2160P、480P 明确拒绝，不静默降档。
- 测试页展示每个入口的用途、图片数量和生成方式。

### 自动门禁

- 网页工程 lane full pytest：`519 passed, 296 subtests passed`。
- Codex 独立最终 full pytest：`519 passed, 1 warning, 296 subtests passed`，耗时 89.81 秒；此前同一候选也曾以 138.75 秒完成同结果复验。
- `python -m compileall -q src tests scripts main.py`：PASS。
- `git diff --check`：PASS；仅有两个既有工作区 LF→CRLF 提示。

### Codex 真实运行验收

- 使用隔离端口 8002 并行提交五项真实请求，统一为 8 秒、16:9、720P；只使用公开测试素材，不读取或输出账号、Cookie、Token、API Key、任务 ID、媒体 URL 或响应正文。
- Omni References：completed，取得 `video/mp4`，1,966,948 bytes。
- Veo Fast 文生：completed，取得 `video/mp4`，4,981,906 bytes。
- Veo Fast 首帧：completed，取得 `video/mp4`，4,088,297 bytes。
- Veo Fast 首尾帧：completed，取得 `video/mp4`，2,673,963 bytes。
- Veo Fast References：completed，取得 `video/mp4`，3,405,734 bytes。
- 首次空 502 已定位为测试客户端错误继承系统代理，导致 localhost 请求未到达 Flow2API；设置测试客户端不继承系统代理后，8002 `/docs` 返回 200，随后五项真实生成全部通过。该问题不是模型、账号登录态或本轮映射失败。
- 临时 8002 服务已关闭并确认无监听。

### 当前运行与交付状态

- 正式 8000 服务仍是 2026-08-17 15:10 启动的旧进程，早于本轮适配器与目录代码修改时间；必须重启一次才能加载本轮新代码。
- 本轮代码仍是本地 dirty 修改：未提交、未推送、未创建 PR、未部署。

## Milestone 17 — 持久 Profile 会话自恢复闭环（2026-08-18）

### 根因与最小修复

- 短期重启后账号仍可生成，但强制执行凭证过期后的持久 Profile 恢复时，真实探针进入 `interactive_verification`；安全检查确认该 Profile 的 Cookie 数据库没有 Flow session cookie，因此旧实现只能依赖数据库中尚未过期的 ST/AT，无法覆盖隔天恢复。
- 最小验证证明：从数据库中已受保护的 Google Cookie 备份里排除旧 Flow session cookie，只把 Google 登录 Cookie 注入该账号的持久 Profile，可自动换取新的 Flow session，并通过账号身份一致性校验。
- 正式实现让 `TokenManager` 在持久 Profile 恢复时传入该账号的 Google Cookie 备份；浏览器恢复入口显式过滤 `__Secure-next-auth.session-token` 与 `next-auth.session-token`，避免复用旧会话，只用 Google 登录态引导新会话。注入失败时仍保留原 Profile 自恢复路径并 fail-closed。

### RED→GREEN 与真实验收

- 两项新增回归先分别以“未知参数”和“恢复链未传 Cookie 备份”失败，随后最小实现使两项均通过。
- 真实强制恢复成功：候选账号由 backoff 自动恢复为 `auth_state=ok`，失败计数归零且没有残留认证错误；无需人工重新登录。
- 服务正常关闭时确认 8000 监听归零且 Flow2API 托管浏览器进程归零；重启后同一账号仍为唯一可生成账号。
- 重启后的真实图片生成返回 HTTP 200 且包含媒体；随后真实 Omni Flash 10 秒视频生成返回 HTTP 200 且包含媒体。

### 最终门禁

- 完整 pytest：`528 passed, 1 warning, 300 subtests passed`；唯一 warning 为既有 Windows `curl_cffi` Proactor 兼容提示。
- `python -m compileall -q src tests scripts`：PASS。
- `git diff --check`：PASS；仅有既有 LF→CRLF 工作区提示。
- 安全扫描：`files_scanned=161`、`forbidden_path_count=5`、`secret_pattern_count=0`。5 个禁止路径均为既有本机运行态边界，不进入交付；未读取或输出凭据值。
- 正式 8000 服务已由当前候选重新启动并保持监听，可继续用于本机验收。
