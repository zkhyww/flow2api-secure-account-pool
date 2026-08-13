# Pluginless account persistence validation

Date: 2026-08-12

## Result

Flow2API personal mode now supports the approved pluginless lifecycle:

- Google account session data is persisted through the existing Windows-current-user DPAPI boundary.
- Service restart restores all configured accounts without extension pairing.
- Browser runtimes start only when generation needs them.
- Each token uses an isolated browser context; cross-token fallback fails closed.
- Idle browser runtimes exit while the Flow2API service remains online.
- The extension path remains available as an advanced fallback.

## Root causes fixed

1. Personal mode read a nonexistent `token.cookie` field instead of `token.google_cookies`.
2. Refreshed browser cookies were written using a nonexistent `cookie` update field.
3. Account onboarding depended on a temporary extension and pairing workflow.
4. A failed new-context creation could fall back to another token's resident context.
5. Resident-tab cleanup did not request shutdown of the final idle browser runtime.
6. Personal-mode UI treated extension connectivity as a required readiness signal.

## RED to GREEN evidence

- Cookie persistence: `3 failed, 1 passed` -> `9 passed` with credential regression tests.
- Native onboarding: `7 failed, 6 passed` -> `13 passed`.
- Cross-account failed-context path: `1 failed` -> isolation/restart/lifecycle `29 passed`.
- Idle runtime cleanup: `1 failed, 12 passed` -> `13 passed`.
- Personal-mode management contract: `6 failed` -> focused and related UI/API contracts passed.

## Independent gates

- Pluginless focused suite: `74 passed`.
- Full repository suite: `225 passed, 58 subtests passed`.
- Python compileall: passed.
- JavaScript syntax checks: passed for `background.js`, `options.js`, and `local-connect.js`.
- `git diff --check`: passed.
- Credential scan: no user credential found. Three matches were reviewed as an upstream public Flow frontend constant (already present in HEAD) and an explicit test fixture.

## Runtime validation

- Captcha mode after restart: `personal`.
- Account count: 3.
- Active account count: 3.
- Persisted-session-ready count: 3.
- Browser process count did not increase at service startup or health check.
- Runtime policy: up to 10 cold browser workers, 5 resident tabs per browser, 60-second idle TTL.
- Cold workers do not start processes; dense reuse is preferred before another browser starts.

## Real Flow validation

Two low-cost single-image checks were executed because the first verified generation and the second verified post-restart persistence plus idle shutdown.

1. Status `succeeded`, HTTP 200, selected token id 1, attempt count 1, `has_media=true`.
2. After service restart: status `succeeded`, HTTP 200, selected token id 1, attempt count 1, `has_media=true`.
3. After 80 seconds idle: browser descendant count 0 while the Flow2API service remained alive.

No prompt, media URL, cookie, session token, access token, profile data, or API key is recorded here.

## Remaining boundary

- A full Windows reboot was not performed. Process restart plus encrypted database reload was verified; the OS-reboot path uses the same persisted database and current-user DPAPI boundary.
- Google may still invalidate sessions or require interactive verification because of account policy or unusual activity. That is an external session-lifetime condition, not a local persistence failure.

## Visible-window correction

User acceptance found that daily personal workers were still configured as headed Chrome. That exposed the internal `/api/auth/providers` bootstrap surface and made temporary incognito windows appear repeatedly. The bootstrap JSON is not a login failure, but exposing it was incorrect and headed default-context tabs did not provide the intended hard account isolation.

The corrected behavior is:

- Daily generation runs in background isolated contexts and injects the selected account's encrypted persisted cookies.
- Native account onboarding alone forces one visible temporary regular profile; it does not use incognito flags.
- Focused regression: `29 passed`.
- Full regression after correction: `227 passed, 58 subtests passed`.
- Service restart: daily runtime `headless=true`; no browser process starts before a task.
