# Video Model Failure Root-Cause and Request-Log QC

Date: 2026-08-13

## Scope and safety boundary

This round investigated the reported video failures in the existing dirty worktree at `D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo`.

Constraints preserved:

- no real generation was executed;
- no Cookie, Token, complete URL, raw prompt, media content, or raw upstream response was read or recorded;
- no change was made to `D:\codex项目\Framefield-Studio`;
- no commit, push, PR, deployment, or service restart;
- existing dirty worktree changes were preserved.

The supplied acceptance evidence was treated as a white-listed observation only. This implementation did not query sensitive runtime data to reproduce it.

## White-listed acceptance evidence used for diagnosis

- account 3 + `gemini-3.1-flash-image-four-three`: completed with media, about 41.7 seconds; the account/model fact subsequently showed available.
- accounts 3 and 2 + `veo_3_1_t2v_fast_landscape_4s`: submit-stage `model_access_denied`, no media, and separate account-scoped unavailable facts.
- account 2 + `veo_3_1_t2v_lite_4s_landscape`: submit-stage `model_access_denied`.
- account 1 + base `veo_3_1_t2v_fast_landscape`: not rejected immediately; later surfaced `upstream_error` with no media, while one request log remained at an interim 102 state instead of being finalized.
- current Flow UI family labels supplied by the acceptance run: Omni Flash, Veo 3.1 Lite, Veo 3.1 Fast, Veo 3.1 Quality.

No real-video success is claimed by this report.

## A. Public-code data-flow trace

### 1. Test page → public model ID

`static/test.html` keeps the selected raw model ID in `STATE.selectedModel` and sends that value as `body.model`. Friendly display names do not replace the request model ID.

### 2. Route → model resolver

`src/core/model_resolver.py` maps video aliases by aspect ratio through `VIDEO_BASE_MODELS`. Short-duration public aliases such as Fast/Lite 4s and 6s resolve to short-duration internal config names.

The resolver currently extracts aspect/image-size style generation parameters, but it has no independent video-duration extraction or propagation path.

### 3. MODEL_CONFIG

`src/services/generation_handler.py` currently has these validated base-family mappings:

- Fast landscape → `veo_3_1_t2v_fast`;
- Lite landscape → `veo_3_1_t2v_lite`;
- Quality landscape → `veo_3_1_t2v`;
- Omni text path → `abra_t2v_8s`, with the UI family label `Omni Flash` retained in config.

The 4s/6s T2V variants are currently constructed differently: duration is encoded into the configured `model_key` suffix. `_make_t2v_config()` itself has no independent duration field.

### 4. Generation handler → Flow client

The T2V handler passes `model_config["model_key"]` and aspect ratio to `FlowClient.generate_video_text()`.

### 5. Flow client request body

`FlowClient.generate_video_text()` currently constructs the private Flow request with `videoModelKey`, aspect ratio, seed, structured text input, metadata, and the existing media/client context. It has no validated independent duration argument or duration field in this request path.

Therefore the current 4s/6s behavior is: the duration distinction reaches the private Flow endpoint only through the suffixed `videoModelKey`.

## B. Duration/model-key adjudication

There is not enough public evidence to safely change the private Flow request contract in this round.

Evidence considered:

- Google Gemini API and Vertex AI public Veo documentation expose video duration independently (`durationSeconds`, values 4/6/8). That establishes the public Veo API concept but does not establish the field name or semantics of Flow's private `batchAsyncGenerateVideoText` request.
- the repository's current private Flow transport has no independent duration slot;
- the public gflow-cli project records a recent live Flow cohort where the duration UI control was absent and the CLI intentionally failed before paid submission rather than guessing a transport parameter.

Conclusion: changing short-duration aliases to base family keys plus an invented private duration field would be guesswork. No 4s/6s production model mapping or Flow request-body change was made.

### Minimum remaining white-listed probe

If a Flow cohort/account exposes a duration selector, the minimum useful probe is a sanitized field-level diff between one base-family submission at default duration and one 4s/6s selection. Preserve only:

- the submitted model-family key value;
- the name and scalar value of any duration-related field;
- whether that field is inside the per-request object, model configuration, or another non-secret request section.

Strip project/account/session identifiers, reCAPTCHA material, prompt text, media identifiers, response bodies, and URLs. If the Flow UI exposes no duration control for the cohort, keep 4s/6s aliases unverified instead of inventing transport fields.

## C. Request-log root cause

### Root cause

The outer `handle_generation()` request lifecycle owns the final request-log update, but video work runs through an inner async generator.

Many video terminal failures follow this sequence:

1. inner video generator marks `generation_result.error_emitted = true`;
2. inner generator yields the terminal error/progress chunk;
3. outer `handle_generation()` yields that chunk to the route/client;
4. only after the consumer requests another item can the outer generator continue into its existing failure-finalization block.

If the browser/SSE consumer stops as soon as it sees the terminal failure, step 4 is never guaranteed to execute. The request log can therefore remain at its latest 102 progress row even though the client already received a terminal failure.

This explains the observed stale interim request-log state without requiring a second scheduler or database bug.

## D. TDD evidence

### RED

Added exact early-close tests before production repair:

- normal video stream: inner generator marks failure and yields a terminal failure chunk; consumer closes immediately;
- idempotent/quota video stream: same early-close boundary;
- direct video child exception remains covered as the control case.

RED command:

`venv/Scripts/python.exe -m pytest tests/test_batch2_regression_gaps.py tests/test_batch3_video_quota_routing.py -q -k "video_terminal_failure_is_logged_before_stream_consumer_closes or video_exception_finalizes_request_log_with_safe_failure or idempotent_video_terminal_failure_is_logged_before_stream_consumer_closes"`

Observed RED:

- normal early-close test: expected final HTTP-class request-log status, got `102`;
- idempotent early-close test: expected final HTTP-class request-log status, got `102`;
- direct exception control already finalized correctly.

Result: `2 failed, 1 passed, 14 deselected`.

### GREEN

Minimal production repair in `src/services/generation_handler.py`:

- added `_finalize_video_failure_log_before_yield()`;
- normal and idempotent video loops call it after `error_emitted` is set and before the first terminal chunk is yielded;
- helper is one-shot per request log;
- persisted failure payload is fixed to the safe allowlist shape: `status=failed`, `error_class=upstream_error`, `has_media=false`;
- no inner/upstream error message, prompt, URL, or media value is persisted by the helper;
- existing direct-exception handling remains unchanged.

Exact GREEN for the three lifecycle tests: `3 passed, 14 deselected`.

## E. Model-family compatibility lock

Added a non-mutating config contract test for the supplied current Flow family set:

- Fast base family remains on its validated base key;
- Lite base family remains on its validated base key and does not acquire synthetic tier upgrade behavior;
- Quality base family remains on its validated base key;
- Omni keeps its existing text key and `Omni Flash` display-family metadata.

This test does not bless the short-duration suffixed keys and does not claim 4s/6s support.

## F. Files changed in this round

Directly edited for this task:

- `src/services/generation_handler.py`
- `tests/test_batch2_regression_gaps.py`
- `tests/test_batch3_video_quota_routing.py`
- `tests/test_veo_lite_support.py`
- `docs/superpowers/reports/2026-08-13-video-model-failure-root-cause-qc.md`

No production change was made to:

- `src/core/model_resolver.py`
- `src/services/flow_client.py`
- `static/test.html`

Those files were read only for the data-flow investigation in this round.

## G. Automated verification

Focused request-log/Veo suite:

- `39 passed, 4 subtests passed`.

Cross-feature focused gate covering Veo config, request-log finalization, failure classification, video quota routing, account/model tri-state, admin test capability, test-page/API-key behavior, three-account routing, and account-onboarding/privacy regressions:

- `96 passed, 21 subtests passed`.

Full gate:

- `271 passed, 76 subtests passed in 58.18s`.

Static verification:

- `venv/Scripts/python.exe -m compileall -q src tests`: exit 0;
- `git diff --check`: exit 0.

Git emitted existing LF/CRLF working-copy warnings only; no whitespace error was reported.

## H. Residual boundary

The request-log lifecycle regression is covered by automated RED→GREEN evidence.

The short-duration video submission failure is not declared fixed. The current evidence is sufficient to say the short-duration path differs from the validated base-family path and currently encodes duration in the model key, but it is not sufficient to name a correct private Flow duration field or to prove that simply using a base key plus a duration scalar is accepted by the current Flow cohort.

A real post-fix video retest remains a Codex acceptance action after a sanitized duration-transport probe resolves that contract.
