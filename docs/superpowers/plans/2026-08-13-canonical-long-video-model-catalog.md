# Canonical Long-Video Model Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose only canonical long-video models through the public/test catalog while preserving legacy request aliases.

**Architecture:** An explicit Python catalog module filters MODEL_CONFIG for discovery and supplies UI metadata. Both model endpoints consume it; the test page consumes endpoint metadata and enforces the existing Extend source-video contract.

**Tech Stack:** Python 3, FastAPI, unittest/pytest, static HTML/JavaScript.

## Global Constraints

- Preserve the current dirty checkout and all unrelated edits.
- No real generation, credentials, complete URLs, raw request/response bodies, prompts, or media.
- No commit, push, PR, issue, deployment, service restart, or other repository.
- Do not change the private Flow request transport or invent a duration field.

---

### Task 1: Capture endpoint and compatibility RED

**Files:**
- Create: `tests/test_canonical_model_catalog.py`

**Interfaces:**
- Consumes: current `/v1/models`, `/api/test/models`, MODEL_CONFIG, and resolve_model_name.
- Produces: exact public video allowlist and representative hidden-alias compatibility contract.

- [ ] Add an ASGI test fixture with existing API-key and test-capability authentication.
- [ ] Assert both endpoints return identical data whose video IDs equal the ten literal canonical IDs.
- [ ] Assert recommended metadata equals the four validated landscape IDs.
- [ ] Assert Extend metadata requires a source video and is not smoke-verified.
- [ ] Assert representative 4s/6s/4K/default/order aliases are absent from discovery but remain in MODEL_CONFIG/resolver.
- [ ] Run `venv/Scripts/python.exe -m pytest tests/test_canonical_model_catalog.py -q`.
- [ ] Confirm failure is caused by the current 182-video enumeration or missing metadata.

### Task 2: Capture test-page RED

**Files:**
- Modify: `tests/test_test_page_contract.py`

**Interfaces:**
- Consumes: static/test.html JavaScript helpers.
- Produces: observable grouping, no-fallback, and Extend eligibility contract.

- [ ] Replace the obsolete short-Fast preference assertions with a literal four-model recommendation assertion.
- [ ] Add a test proving the embedded legacy fallback catalog and 4s/6s preference labels are gone.
- [ ] Add a Node-executed pure-helper test:
  - ordinary model is eligible;
  - Extend without mediaGenerationId is ineligible;
  - Extend with mediaGenerationId is eligible.
- [ ] Assert generation builds the existing `extend://` content item only for source-required entries.
- [ ] Run the two focused test files and confirm page-contract failures are caused by old behavior.

### Task 3: Implement one public catalog

**Files:**
- Create: `src/core/public_model_catalog.py`
- Modify: `src/api/routes.py`

**Interfaces:**
- Produces: `build_public_model_catalog(model_config: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]`.

- [ ] Define the ten ordered video specs and four recommended IDs as literals.
- [ ] Build detached entries; validate each canonical ID exists and is video.
- [ ] Append existing image configurations without altering their IDs or routing.
- [ ] Replace `_get_openai_model_catalog` enumeration with the builder.
- [ ] Return the same complete entry shape from both endpoints.
- [ ] Run `tests/test_canonical_model_catalog.py` to GREEN.
- [ ] Run existing resolver/Veo tests to prove compatibility remains GREEN.

### Task 4: Make the test page catalog-driven and Extend-safe

**Files:**
- Modify: `static/test.html`

**Interfaces:**
- Consumes: endpoint fields `id`, `description`, `model_type`, `display_name`, `recommended`, `catalog_order`, `requires_video_id`.
- Produces: fixed recommended group and `canGenerateSelectedModel(modelMeta, mediaGenerationId)`.

- [ ] Remove the embedded fallback model map and old short-duration labels.
- [ ] Store complete API entries and sort by catalog_order.
- [ ] Group on recommended metadata while retaining per-account green/red/yellow badges.
- [ ] Add a hidden mediaGenerationId form group, show it for Extend, and refresh button eligibility on input.
- [ ] Guard generate() against missing source ID and add the existing `extend://` content item when present.
- [ ] Run the focused page and catalog tests to GREEN.

### Task 5: Documentation and fresh verification

**Files:**
- Modify: `README.md`
- Create: `docs/superpowers/reports/2026-08-13-canonical-long-video-model-catalog-qc.md`

**Interfaces:**
- Produces: human-facing catalog/compatibility/risk documentation and evidence.

- [ ] Replace the public-video tables with the ten canonical entries.
- [ ] State one generation is 8 seconds; longer videos require Extend and source mediaGenerationId.
- [ ] State hidden aliases remain callable but are not public catalog entries.
- [ ] State Extend has not received a real smoke.
- [ ] Record exact RED and GREEN results without sensitive payloads.
- [ ] Run focused suites.
- [ ] Run `venv/Scripts/python.exe -m pytest -q`.
- [ ] Run `venv/Scripts/python.exe -m compileall -q src tests`.
- [ ] Run `git diff --check`.
- [ ] Review `git status --short` and scoped diff names; do not commit.
