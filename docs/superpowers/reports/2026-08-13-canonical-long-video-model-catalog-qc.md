# Canonical Long-Video Model Catalog QC

Date: 2026-08-13

## Scope and safety boundary

This change was implemented in the existing dirty checkout at
`D:\CodexWorkspaces\Flow2API-Secure-Account-Pool\repo`.

The following constraints were preserved:

- all existing dirty worktree changes were kept;
- no other repository was modified;
- no real generation was executed;
- no Cookie, Token, account email, complete URL, prompt, request/response body, or media was read or recorded;
- no commit, push, PR, issue, deployment, or service restart was performed;
- account-pool, security, scheduling, and private Flow generation transport behavior were not rewritten.

Only the supplied white-list facts were used for validation claims.

## Result

`/v1/models`, `/api/test/models`, and the model test page now share one canonical public catalog contract.

The public video catalog contains exactly these ten IDs, in order:

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

The first four are the recommended group because they are the four supplied completed, media-bearing, 8.0-second landscape entrypoints. Account-scoped green/red/yellow availability remains separately rendered and does not alter this canonical recommendation order.

The 30 existing concrete image configurations remain in discovery. The reduction is therefore from 212 total MODEL_CONFIG entries to 40 discoverable entries: 30 images plus 10 canonical videos.

## Single source of truth

`src/core/public_model_catalog.py` owns:

- the ordered ten-video allowlist;
- display names and descriptions;
- the four global recommendation flags;
- validation status;
- source-video requirements;
- model type and image-support metadata used by the test page.

Both model endpoints use `build_public_model_catalog(MODEL_CONFIG)` through the same route helper and return the same detached resource data after their existing, distinct authentication gates.

The test page no longer embeds a second fallback model list. A catalog load failure now displays an empty/error state rather than resurrecting obsolete 4s/6s and upsample aliases.

## TDD evidence

### RED

Tests were written before production changes in:

- `tests/test_canonical_model_catalog.py`;
- `tests/test_test_page_contract.py`.

RED command:

`venv/Scripts/python.exe -m pytest tests/test_canonical_model_catalog.py tests/test_test_page_contract.py -q`

Observed RED:

- `7 failed, 12 passed`.

The failures proved:

- both `/v1/models` and `/api/test/models` still exposed the full video compatibility table;
- hidden aliases were still discoverable;
- the endpoints lacked recommendation and Extend metadata;
- the test page still contained its large legacy fallback catalog;
- 4s/6s Fast models were still preferred;
- portrait/Extend models entered the old fallback recommendation group;
- there was no `canGenerateSelectedModel()` source-video gate.

### GREEN

After the minimal catalog/API/page implementation, the same focused command produced:

- `19 passed`.

The wider focused gate produced:

- `77 passed, 12 subtests passed in 11.95s`.

Covered areas include canonical discovery, page behavior, API/test authentication, existing Veo/resolver compatibility, account-scoped model availability, and existing page security/connectivity contracts.

## Test-page behavior

The page now:

- stores the complete API catalog entry instead of only a description string;
- sorts by server-provided catalog order;
- uses server-provided display, recommendation, type, validation, image, and Extend metadata;
- keeps account-specific availability badges:
  - green: verified available;
  - red: unavailable for the selected account;
  - yellow: not yet verified;
- shows the four validated 8-second landscape entrypoints in the recommendation group;
- labels Extend as continuation requiring a source video;
- exposes a `mediaGenerationId` field only for Extend;
- keeps the generation button disabled while that field is empty;
- uses the existing `extend://` request convention only after a source ID is supplied.

No media ID is logged or included in this report.

## Compatibility boundary

No MODEL_CONFIG entry was removed. No VIDEO_BASE_MODELS resolver entry was removed.

Representative tests prove that hidden names still remain configured and route, including:

- a default-duration name;
- a 4-second alias;
- a 6-second alias;
- a 4K derivative;
- an alternate `8s_landscape` ordering alias.

Therefore existing callers may continue to send old default, 4s, 6s, 1080P/4K, and duplicate-order aliases. The change affects public discovery and the test page only; it does not delete legacy request compatibility.

Gemini-compatible `/models` and `/v1beta/models` discovery were not changed in this scoped task.

## Duration and Extend risk

The private T2V request contract has only been validated to carry `videoModelKey`; no independent duration field has been validated. This implementation does not invent or send a duration field.

The public catalog consequently declares one generation as 8 seconds. Longer videos require Extend continuation.

Extend remains a known risk:

- it has not received a real smoke;
- it requires an existing source video `mediaGenerationId`;
- it is marked `unverified`;
- the test page cannot submit it as an ordinary text-to-video request without the source ID.

No production validation claim is made for Extend or for portrait variants.

## Exact files changed for this catalog task

Production and UI:

- `src/core/public_model_catalog.py` — new single public catalog/filter source;
- `src/api/routes.py` — both requested model endpoints consume the catalog;
- `static/test.html` — removes legacy fallback and enforces Extend source input;
- `README.md` — documents canonical 8-second discovery, compatibility, and Extend risk.

Tests:

- `tests/test_canonical_model_catalog.py` — new endpoint/filter/compatibility contract;
- `tests/test_test_page_contract.py` — canonical grouping and Extend page contract.

Task documentation:

- `docs/superpowers/specs/2026-08-13-canonical-long-video-model-catalog-design.md`;
- `docs/superpowers/plans/2026-08-13-canonical-long-video-model-catalog.md`;
- `docs/superpowers/reports/2026-08-13-canonical-long-video-model-catalog-qc.md`.

Other dirty files shown by Git predated this scoped catalog change and were preserved.

## Final verification

Fresh final gates:

- focused catalog/page/cross-feature suite:
  `77 passed, 12 subtests passed in 11.95s`;
- full pytest:
  `278 passed, 76 subtests passed in 53.78s`;
- `venv/Scripts/python.exe -m compileall -q src tests`: exit 0;
- whole-page JavaScript parse using Node: exit 0;
- `git diff --check`: exit 0.

Git emitted existing LF/CRLF working-copy warnings only. No whitespace error was reported.
