# Canonical Long-Video Model Catalog Design

Date: 2026-08-13

## Goal

Replace the 212-entry public/test-page model enumeration with one explicit public catalog that exposes the existing 30 image configurations plus exactly ten canonical long-video entries, while retaining every existing MODEL_CONFIG and resolver alias for request compatibility.

## Safety and scope

- Work in the existing dirty checkout and preserve all prior changes.
- Do not modify account-pool, credential, security, scheduling, or generation transport behavior.
- Do not invent a private Flow duration field. The validated single-generation ceiling is 8 seconds.
- Do not run real generation or read runtime credentials, prompts, response bodies, complete URLs, or media.
- Do not commit, push, open a PR/issue, deploy, or modify another repository.

## Public video contract

The only public video IDs are, in catalog order:

1. `omni`
2. `veo_3_1_t2v_landscape_8s`
3. `veo_3_1_t2v_lite_landscape_8s`
4. `veo_3_1_t2v_fast_landscape_8s`
5. `omni_portrait`
6. `veo_3_1_t2v_portrait_8s`
7. `veo_3_1_t2v_lite_portrait_8s`
8. `veo_3_1_t2v_fast_portrait_8s`
9. `veo_3_1_extend`
10. `veo_3_1_extend_portrait`

The first four entries are the globally recommended, white-list-validated 8-second landscape entrypoints. Account-scoped availability remains an independent green/red/yellow badge and does not rewrite the global validation fact.

The two Extend entries are visibly marked as continuation models that require a source video mediaGenerationId. Extend is not described as smoke-verified.

## Architecture

Create `src/core/public_model_catalog.py` as the sole public filtering and metadata source. It owns the ordered video allowlist and builds detached catalog entries from the existing MODEL_CONFIG without mutating it. Image configurations remain publicly listed as today. All other video configurations remain routable but are hidden from public discovery.

Both `/v1/models` and `/api/test/models` call the same catalog builder and return identical model data after their existing authentication gates. Each entry includes stable display metadata used by the test page: type, display name, recommendation flag/order, source-video requirement, and global validation status.

The test page removes the stale embedded fallback catalog. It renders only API-supplied entries, uses server metadata for grouping and labels, and keeps the existing account-scoped availability badges. Extend selection reveals a mediaGenerationId input; generation remains disabled until a non-empty value exists, then uses the already-supported `extend://` request convention.

## Compatibility boundary

MODEL_CONFIG and VIDEO_BASE_MODELS are not reduced. Representative default, 4s, 6s, upsample, and duplicate-order aliases remain accepted by the existing resolver/generation path. Hiding an alias affects discovery only.

Gemini-compatible `/models` and `/v1beta/models` are outside this scoped catalog change because the requested contracts are `/v1/models`, `/api/test/models`, and the test page.

## Testing

- Endpoint tests assert the exact ten public video IDs, identical API/test data, required metadata, and exclusion of short/upsample/duplicate aliases.
- Compatibility tests assert representative hidden aliases still exist and resolve.
- Page behavior tests execute the grouping and Extend eligibility helpers, assert the four recommended entries, preserve account status rendering, and prove missing mediaGenerationId cannot submit Extend.
- Run focused tests first, then full pytest, compileall, and git diff --check.

## Documentation

README describes canonical public video discovery, 8-second single generations, Extend for longer continuation, hidden compatibility aliases, and Extend's unsmoked risk. The QC report records RED/GREEN evidence and final gates.
