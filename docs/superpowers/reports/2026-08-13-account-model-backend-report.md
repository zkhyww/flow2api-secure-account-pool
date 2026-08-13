# Account Model Availability — Backend Report

Date: 2026-08-13

## Scope completed

Implemented plan Tasks 1–3 only.

- Added the independent `account_model_availability` SQLite fact table, keyed by `(token_id, model)`.
- Added safe, idempotent task-metadata backfill. Its SQL selects only `token_id`, `model`, `status`, `has_media`, and `error_class`; it never reads `prompt` or `result_urls`.
- Added database interfaces for recording available/unavailable facts and reading a single account's allowlisted availability facts.
- Recorded `available` only for successful generations with media.
- Recorded `unavailable` only for `model_access_denied` and `membership_tier`; transient failures leave a prior fact unchanged. A later media success overwrites an unavailable fact to available.
- Added protected `GET /api/test/model-availability?diagnostic_token_id=...`, which validates the diagnostic account and returns only `model`, `status`, `error_class`, and `last_verified_at`.

No real generation, commit, push, PR, deployment, frontend, static test page, or Framefield change was performed for this work.

## TDD evidence

### RED

1. `venv\\Scripts\\python.exe -m pytest tests\\test_account_model_availability.py -q`
   - Failed as expected before Task 1 implementation: missing `Database.record_account_model_unavailable` and `Database.get_account_model_availability`.
2. `venv\\Scripts\\python.exe -m pytest tests\\test_account_model_availability.py -q`
   - Failed as expected before Task 2 implementation: successful media generation left no account/model availability record.
3. `venv\\Scripts\\python.exe -m pytest tests\\test_admin_test_capability.py -q`
   - Failed as expected before Task 3 implementation: the protected availability endpoint was unregistered (`404` instead of the expected capability rejection).

### GREEN

- `venv\\Scripts\\python.exe -m pytest tests\\test_account_model_availability.py tests\\test_admin_test_capability.py -q`
  - `12 passed in 2.63s`
- `venv\\Scripts\\python.exe -m pytest tests\\test_batch2_failure_classes.py tests\\test_batch2_idempotency_boundaries.py tests\\test_batch3_failure_routing.py tests\\test_batch3_quota_reservation.py tests\\test_batch3_video_quota_routing.py tests\\test_three_account_routing.py -q`
  - `40 passed, 9 subtests passed in 19.95s`
- `venv\\Scripts\\python.exe -m compileall -q src tests`
  - Passed.
- `git diff --check`
  - Passed.

## Modified files

- `src/core/database.py`
- `src/services/generation_handler.py`
- `src/api/routes.py`
- `tests/test_account_model_availability.py`
- `tests/test_admin_test_capability.py`

## Remaining risks

- The full test suite was not run; only the new focused tests and generation/routing regressions listed above were run.
- Existing uncommitted changes in the working tree were preserved. `src/api/routes.py`, `src/services/generation_handler.py`, and `tests/test_admin_test_capability.py` already contained unrelated in-progress changes; this implementation was added incrementally on top of them.
