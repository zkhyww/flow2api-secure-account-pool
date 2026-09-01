import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin
from src.services.account_profile_store import AccountProfileStore


class AccountReauthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_db = admin.db
        self.original_manager = admin.token_manager
        self.original_store = admin.account_profile_store
        self._temp_dir = tempfile.TemporaryDirectory()
        self.store = AccountProfileStore(Path(self._temp_dir.name) / "profiles")
        admin.account_profile_store = self.store

    async def asyncTearDown(self):
        admin.db = self.original_db
        admin.token_manager = self.original_manager
        admin.account_profile_store = self.original_store
        self._temp_dir.cleanup()

    async def test_reauth_clones_existing_profile_then_replaces_reference_after_success(self):
        existing_key = "a" * 32
        target = SimpleNamespace(
            id=7,
            email="expected@example.invalid",
            account_profile_key=existing_key,
        )
        existing_dir = self.store.resolve(existing_key, create=True)
        (existing_dir / "synthetic-state.txt").write_text("existing-login-state", encoding="utf-8")
        admin.db = SimpleNamespace(get_token=AsyncMock(return_value=target))
        admin.token_manager = SimpleNamespace(_mark_auth_failure=AsyncMock())
        captured = {}

        class FakeBrowser:
            def __init__(self, *_args, **kwargs):
                captured["kwargs"] = dict(kwargs)
                self.initialize = AsyncMock()
                self.capture_account_onboarding_result = AsyncMock(
                    return_value={
                        "st": "synthetic-session",
                        "google_cookies": "synthetic-cookie",
                    }
                )
                self.close = AsyncMock()
                captured["instance"] = self

        persist = AsyncMock(return_value="success")
        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            new=FakeBrowser,
        ), patch.object(
            admin,
            "_persist_native_reauth_result",
            new=persist,
        ):
            result = await admin._run_account_reauth(7)

        self.assertEqual({"success": True, "auth_status": "正常"}, result)
        candidate_key = persist.await_args.kwargs["account_profile_key"]
        self.assertNotEqual(existing_key, candidate_key)
        self.assertRegex(candidate_key, r"^[0-9a-f]{32}$")
        candidate_dir = self.store.resolve(candidate_key)
        self.assertEqual(
            "existing-login-state",
            (candidate_dir / "synthetic-state.txt").read_text(encoding="utf-8"),
        )
        self.assertFalse(self.store.exists(existing_key))
        self.assertTrue(captured["kwargs"]["force_headed"])
        self.assertEqual(candidate_dir, captured["kwargs"]["persistent_profile_dir"])
        captured["instance"].close.assert_awaited_once()

    async def test_identity_mismatch_preserves_existing_profile_and_removes_candidate(self):
        existing_key = "a" * 32
        target = SimpleNamespace(
            id=7,
            email="expected@example.invalid",
            account_profile_key=existing_key,
        )
        existing_dir = self.store.resolve(existing_key, create=True)
        (existing_dir / "synthetic-state.txt").write_text("keep-me", encoding="utf-8")
        admin.db = SimpleNamespace(get_token=AsyncMock(return_value=target))
        admin.token_manager = SimpleNamespace(_mark_auth_failure=AsyncMock())
        captured = {}

        class FakeBrowser:
            def __init__(self, *_args, **kwargs):
                captured["persistent_profile_dir"] = kwargs["persistent_profile_dir"]
                self.initialize = AsyncMock()
                self.capture_account_onboarding_result = AsyncMock(
                    return_value={
                        "st": "synthetic-session-other",
                        "google_cookies": "synthetic-cookie-other",
                    }
                )
                self.close = AsyncMock()
                captured["instance"] = self

        persist = AsyncMock(side_effect=ValueError("identity_mismatch"))
        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            new=FakeBrowser,
        ), patch.object(
            admin,
            "_persist_native_reauth_result",
            new=persist,
        ):
            result = await admin._run_account_reauth(7)

        self.assertEqual({"success": False, "auth_status": "需要重新登录"}, result)
        self.assertNotIn("profile", str(result).lower())
        candidate_key = persist.await_args.kwargs["account_profile_key"]
        self.assertTrue(self.store.exists(existing_key))
        self.assertEqual(
            "keep-me",
            (self.store.resolve(existing_key) / "synthetic-state.txt").read_text(encoding="utf-8"),
        )
        self.assertFalse(self.store.exists(candidate_key))
        captured["instance"].close.assert_awaited_once()

    async def test_browser_failure_removes_empty_candidate_and_returns_retry_state(self):
        target = SimpleNamespace(
            id=7,
            email="expected@example.invalid",
            account_profile_key="",
        )
        admin.db = SimpleNamespace(get_token=AsyncMock(return_value=target))
        manager = SimpleNamespace(_mark_auth_failure=AsyncMock())
        admin.token_manager = manager
        captured = {}

        class FakeBrowser:
            def __init__(self, *_args, **kwargs):
                captured["persistent_profile_dir"] = kwargs["persistent_profile_dir"]
                self.initialize = AsyncMock(side_effect=RuntimeError("synthetic"))
                self.capture_account_onboarding_result = AsyncMock()
                self.close = AsyncMock()
                captured["instance"] = self

        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            new=FakeBrowser,
        ):
            result = await admin._run_account_reauth(7)

        self.assertEqual({"success": False, "auth_status": "稍后重试"}, result)
        manager._mark_auth_failure.assert_awaited_once_with(
            7,
            "browser_start_failed",
            interactive=False,
        )
        self.assertEqual([], list(self.store.root.glob("*")))
        captured["instance"].close.assert_awaited_once()

    async def test_post_start_capture_failure_keeps_safe_stage_and_exception_type(self):
        target = SimpleNamespace(
            id=7,
            email="expected@example.invalid",
            account_profile_key="",
        )
        admin.db = SimpleNamespace(get_token=AsyncMock(return_value=target))
        manager = SimpleNamespace(_mark_auth_failure=AsyncMock())
        admin.token_manager = manager
        captured = {}
        sentinel = "SENSITIVE_CAPTURE_URL_TOKEN_PROFILE_RESPONSE"

        class SyntheticCaptureFailure(RuntimeError):
            pass

        class FakeBrowser:
            def __init__(self, *_args, **_kwargs):
                self.initialize = AsyncMock()
                self.capture_account_onboarding_result = AsyncMock(
                    side_effect=SyntheticCaptureFailure(sentinel)
                )
                self.close = AsyncMock()
                captured["instance"] = self

        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            new=FakeBrowser,
        ), patch(
            "src.core.logger.debug_logger.log_warning"
        ) as warning_log:
            result = await admin._run_account_reauth(7)

        self.assertEqual({"success": False, "auth_status": "稍后重试"}, result)
        manager._mark_auth_failure.assert_awaited_once_with(
            7,
            "network",
            interactive=False,
        )
        captured["instance"].initialize.assert_awaited_once()
        log_text = str(warning_log.call_args_list)
        self.assertIn("stage=capture", log_text)
        self.assertIn("exception_type=SyntheticCaptureFailure", log_text)
        self.assertNotIn(sentinel, log_text)
        self.assertNotIn("expected@example.invalid", log_text)

    async def test_close_failure_does_not_override_interactive_capture_failure(self):
        target = SimpleNamespace(
            id=7,
            email="expected@example.invalid",
            account_profile_key="",
        )
        admin.db = SimpleNamespace(get_token=AsyncMock(return_value=target))
        manager = SimpleNamespace(_mark_auth_failure=AsyncMock())
        admin.token_manager = manager
        sentinel = "SENSITIVE_CLOSE_URL_TOKEN_PROFILE_RESPONSE"

        class SyntheticCloseFailure(RuntimeError):
            pass

        browser = SimpleNamespace(
            initialize=AsyncMock(),
            capture_account_onboarding_result=AsyncMock(side_effect=TimeoutError()),
            close=AsyncMock(side_effect=SyntheticCloseFailure(sentinel)),
        )

        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            return_value=browser,
        ), patch(
            "src.core.logger.debug_logger.log_warning"
        ) as warning_log:
            result = await admin._run_account_reauth(7)

        self.assertEqual({"success": False, "auth_status": "需要重新登录"}, result)
        manager._mark_auth_failure.assert_awaited_once_with(
            7,
            "interactive_verification",
            interactive=True,
        )
        log_text = str(warning_log.call_args_list)
        self.assertIn("stage=close", log_text)
        self.assertIn("exception_type=SyntheticCloseFailure", log_text)
        self.assertNotIn(sentinel, log_text)
        self.assertNotIn("expected@example.invalid", log_text)

    async def test_post_write_failure_preserves_old_profile_reference_and_cleans_candidate(self):
        existing_key = "b" * 32
        target = SimpleNamespace(
            id=9,
            email="expected@example.invalid",
            account_profile_key=existing_key,
        )
        self.store.resolve(existing_key, create=True)

        async def legacy_update_token(*_args, **kwargs):
            target.account_profile_key = kwargs.get(
                "account_profile_key",
                target.account_profile_key,
            )

        admin.db = SimpleNamespace(
            get_token=AsyncMock(return_value=target),
            commit_account_reauth=AsyncMock(
                side_effect=RuntimeError("synthetic-atomic-write-failure")
            ),
        )
        manager = SimpleNamespace(
            flow_client=SimpleNamespace(
                st_to_at=AsyncMock(
                    return_value={
                        "access_token": "fixture",
                        "user": {"email": "expected@example.invalid"},
                    }
                ),
                get_credits=AsyncMock(return_value={"credits": 100}),
            ),
            update_token=AsyncMock(side_effect=legacy_update_token),
            _mark_auth_success=AsyncMock(
                side_effect=RuntimeError("synthetic-post-write-failure")
            ),
            enable_token=AsyncMock(),
            _mark_auth_failure=AsyncMock(),
        )
        admin.token_manager = manager
        browser = SimpleNamespace(
            initialize=AsyncMock(),
            capture_account_onboarding_result=AsyncMock(
                return_value={"st": "fixture", "google_cookies": "fixture"}
            ),
            close=AsyncMock(),
        )

        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            return_value=browser,
        ):
            result = await admin._run_account_reauth(9)

        self.assertEqual({"success": False, "auth_status": "稍后重试"}, result)
        self.assertEqual(existing_key, target.account_profile_key)
        self.assertTrue(self.store.exists(existing_key))
        self.assertEqual({existing_key}, {path.name for path in self.store.root.iterdir()})

    async def test_reauth_endpoint_requires_admin_and_returns_stable_payload(self):
        admin_token = "reauth-admin-fixture"
        admin.active_admin_tokens.add(admin_token)
        app = FastAPI()
        app.include_router(admin.router)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        runner = AsyncMock(return_value={"success": True, "auth_status": "正常"})
        try:
            with patch.object(admin, "_run_account_reauth", new=runner, create=True):
                unauthorized = await client.post("/api/tokens/7/reauth")
                authorized = await client.post(
                    "/api/tokens/7/reauth",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
        finally:
            admin.active_admin_tokens.discard(admin_token)
            await client.aclose()

        self.assertEqual(401, unauthorized.status_code)
        self.assertEqual(200, authorized.status_code)
        self.assertEqual({"success": True, "auth_status": "正常"}, authorized.json())
        runner.assert_awaited_once_with(7)


if __name__ == "__main__":
    unittest.main()
