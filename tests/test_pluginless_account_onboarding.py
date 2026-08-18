import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.api import admin
from src.services.browser_captcha_personal import BrowserCaptchaService


class PluginlessBrowserCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_recovery_bootstrap_excludes_stale_session_cookie(self):
        service = BrowserCaptchaService(force_headed=True)
        service.initialize = AsyncMock()
        service._set_browser_cookie_targets = AsyncMock(return_value=1)
        service._open_visible_browser_tab = AsyncMock(return_value=object())
        service._get_browser_cookies = AsyncMock(
            return_value=[
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "fresh-session",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        )

        result = await service.capture_account_onboarding_result(
            timeout_seconds=1,
            poll_interval_seconds=0.001,
            bootstrap_google_cookies=json.dumps(
                [
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "stale-session",
                        "domain": ".google.com",
                        "path": "/",
                    },
                    {
                        "name": "SID",
                        "value": "durable-google-login",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ]
            ),
        )

        self.assertEqual("fresh-session", result["st"])
        seeded = service._set_browser_cookie_targets.await_args.args[0]
        seeded_names = {str(cookie.get("name") or "") for cookie in seeded}
        self.assertIn("SID", seeded_names)
        self.assertNotIn("__Secure-next-auth.session-token", seeded_names)
        self.assertNotIn("next-auth.session-token", seeded_names)

    async def test_completed_login_returns_only_normalized_private_session_state(self):
        service = BrowserCaptchaService(force_headed=True)
        service.initialize = AsyncMock()
        service._open_visible_browser_tab = AsyncMock(return_value=object())
        service._get_browser_cookies = AsyncMock(
            return_value=[
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "opaque-session-native",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                },
                {
                    "name": "SID",
                    "value": "opaque-cookie-native",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                },
            ]
        )

        result = await service.capture_account_onboarding_result(
            timeout_seconds=1,
            poll_interval_seconds=0.001,
        )

        cookie_items = json.loads(result["google_cookies"])
        self.assertEqual({"st", "google_cookies"}, set(result))
        self.assertEqual("opaque-session-native", result["st"])
        self.assertEqual(
            {
                "__Secure-next-auth.session-token": "opaque-session-native",
                "SID": "opaque-cookie-native",
            },
            {item["name"]: item["value"] for item in cookie_items},
        )
        self.assertIsNone(service._runtime_extension_directory)
        service._open_visible_browser_tab.assert_awaited_once()

    async def test_persistent_profile_capture_keeps_profile_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_dir = Path(temp_dir) / "profile"
            profile_dir.mkdir()
            service = BrowserCaptchaService(
                force_headed=True,
                persistent_profile_dir=profile_dir,
            )
            service.initialize = AsyncMock()
            service._open_visible_browser_tab = AsyncMock(return_value=object())
            service._get_browser_cookies = AsyncMock(
                return_value=[
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "synthetic-session",
                        "domain": ".google.com",
                        "path": "/",
                    }
                ]
            )

            result = await service.capture_account_onboarding_result(
                timeout_seconds=1,
                poll_interval_seconds=0.001,
            )

            self.assertEqual({"st", "google_cookies"}, set(result))
            self.assertNotIn(str(profile_dir), json.dumps(result))

    async def test_incomplete_login_times_out_without_returning_partial_cookies(self):
        service = BrowserCaptchaService(force_headed=True)
        service.initialize = AsyncMock()
        service._open_visible_browser_tab = AsyncMock(return_value=object())
        service._get_browser_cookies = AsyncMock(
            return_value=[
                {
                    "name": "SID",
                    "value": "opaque-cookie-without-session",
                    "domain": ".google.com",
                    "path": "/",
                }
            ]
        )

        with self.assertRaises(TimeoutError):
            await service.capture_account_onboarding_result(
                timeout_seconds=0.02,
                poll_interval_seconds=0.005,
            )


class PluginlessAccountPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_token_manager = admin.token_manager
        self.original_db = admin.db

    async def asyncTearDown(self):
        admin.token_manager = self.original_token_manager
        admin.db = self.original_db

    async def test_native_capture_adds_account_through_token_manager(self):
        flow_client = SimpleNamespace(
            st_to_at=AsyncMock(
                return_value={
                    "access_token": "opaque-access-native",
                    "expires": "2030-01-01T00:00:00Z",
                    "user": {"email": "fixture-new@example.invalid"},
                }
            )
        )
        manager = SimpleNamespace(
            flow_client=flow_client,
            add_token=AsyncMock(return_value=SimpleNamespace(id=11)),
            update_token=AsyncMock(),
            disable_token=AsyncMock(),
            _mark_auth_success=AsyncMock(),
        )
        database = SimpleNamespace(
            get_token_by_email=AsyncMock(return_value=None),
            update_token=AsyncMock(),
        )
        admin.token_manager = manager
        admin.db = database
        private_result = {
            "st": "opaque-session-native",
            "google_cookies": "SID=opaque-cookie-native",
        }

        profile_key = "b" * 32
        outcome = await admin._persist_native_onboarding_result(
            private_result,
            account_profile_key=profile_key,
        )

        self.assertEqual("success", outcome)
        manager.add_token.assert_awaited_once_with(
            st="opaque-session-native",
            remark="Added from native browser login",
            protocol_mode="protocol",
            google_cookies="SID=opaque-cookie-native",
            account_profile_key="",
        )
        database.update_token.assert_awaited_once_with(
            11,
            account_profile_key=profile_key,
        )
        manager.update_token.assert_not_awaited()
        manager.disable_token.assert_not_awaited()

    async def test_native_capture_updates_existing_account_without_changing_enabled_state(self):
        existing = SimpleNamespace(id=7, is_active=False)
        flow_client = SimpleNamespace(
            st_to_at=AsyncMock(
                return_value={
                    "access_token": "opaque-access-native",
                    "expires": "2030-01-01T00:00:00Z",
                    "user": {"email": "fixture-existing@example.invalid"},
                }
            )
        )
        manager = SimpleNamespace(
            flow_client=flow_client,
            add_token=AsyncMock(),
            update_token=AsyncMock(),
            disable_token=AsyncMock(),
            _mark_auth_success=AsyncMock(),
            _clear_at_validation_cache=Mock(),
        )
        database = SimpleNamespace(
            get_token_by_email=AsyncMock(return_value=existing),
            update_token=AsyncMock(),
        )
        admin.token_manager = manager
        admin.db = database

        profile_key = "c" * 32
        outcome = await admin._persist_native_onboarding_result(
            {
                "st": "opaque-session-updated",
                "google_cookies": "SID=opaque-cookie-updated",
            },
            account_profile_key=profile_key,
        )

        self.assertEqual("updated", outcome)
        database.update_token.assert_awaited_once()
        update_args = database.update_token.await_args.args
        update_kwargs = database.update_token.await_args.kwargs
        self.assertEqual((7,), update_args)
        self.assertEqual("opaque-session-updated", update_kwargs["st"])
        self.assertEqual("opaque-access-native", update_kwargs["at"])
        self.assertEqual("protocol", update_kwargs["protocol_mode"])
        self.assertEqual(
            "SID=opaque-cookie-updated",
            update_kwargs["google_cookies"],
        )
        self.assertEqual(profile_key, update_kwargs["account_profile_key"])
        self.assertEqual("ok", update_kwargs["auth_state"])
        self.assertEqual(0, update_kwargs["auth_failure_count"])
        self.assertIsNone(update_kwargs["auth_next_retry_at"])
        self.assertEqual("", update_kwargs["last_auth_error_class"])
        manager._clear_at_validation_cache.assert_called_once_with(7)
        manager.update_token.assert_not_awaited()
        manager.disable_token.assert_not_awaited()
        manager._mark_auth_success.assert_not_awaited()
        manager.add_token.assert_not_awaited()

    async def test_native_capture_rejects_incomplete_private_state_before_persistence(self):
        admin.token_manager = SimpleNamespace(
            flow_client=SimpleNamespace(st_to_at=AsyncMock()),
            add_token=AsyncMock(),
            update_token=AsyncMock(),
        )
        admin.db = SimpleNamespace(get_token_by_email=AsyncMock())

        with self.assertRaises(ValueError):
            await admin._persist_native_onboarding_result(
                {"google_cookies": "SID=opaque-cookie-only"},
                account_profile_key="d" * 32,
            )

        admin.token_manager.flow_client.st_to_at.assert_not_awaited()
        admin.token_manager.add_token.assert_not_awaited()
        admin.token_manager.update_token.assert_not_awaited()

    async def test_reauth_identity_mismatch_fails_closed_without_enabling_or_overwrite(self):
        existing = SimpleNamespace(
            id=7,
            email="expected@example.invalid",
            is_active=False,
            account_profile_key="e" * 32,
        )
        manager = SimpleNamespace(
            flow_client=SimpleNamespace(
                st_to_at=AsyncMock(
                    return_value={
                        "access_token": "opaque-access-other",
                        "user": {"email": "other@example.invalid"},
                    }
                )
            ),
            update_token=AsyncMock(),
            enable_token=AsyncMock(),
            _mark_auth_success=AsyncMock(),
            _mark_auth_failure=AsyncMock(),
        )
        admin.token_manager = manager
        admin.db = SimpleNamespace(get_token=AsyncMock(return_value=existing))

        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            await admin._persist_native_reauth_result(
                7,
                {
                    "st": "opaque-session-other",
                    "google_cookies": "opaque-cookie-other",
                },
                account_profile_key="f" * 32,
            )

        manager.update_token.assert_not_awaited()
        manager.enable_token.assert_not_awaited()
        manager._mark_auth_success.assert_not_awaited()
        manager._mark_auth_failure.assert_awaited_once_with(
            7,
            "identity_mismatch",
            interactive=True,
        )

    async def test_reauth_matching_identity_persists_profile_then_explicitly_enables(self):
        existing = SimpleNamespace(
            id=7,
            email="expected@example.invalid",
            is_active=False,
            account_profile_key="",
        )
        manager = SimpleNamespace(
            flow_client=SimpleNamespace(
                st_to_at=AsyncMock(
                    return_value={
                        "access_token": "opaque-access-matching",
                        "expires": "2030-01-01T00:00:00Z",
                        "user": {"email": "expected@example.invalid"},
                    }
                )
            ),
            update_token=AsyncMock(),
            enable_token=AsyncMock(),
            _mark_auth_success=AsyncMock(),
            _mark_auth_failure=AsyncMock(),
        )
        admin.token_manager = manager
        commit_reauth = AsyncMock()
        admin.db = SimpleNamespace(
            get_token=AsyncMock(return_value=existing),
            commit_account_reauth=commit_reauth,
        )
        profile_key = "1" * 32

        outcome = await admin._persist_native_reauth_result(
            7,
            {
                "st": "opaque-session-matching",
                "google_cookies": "opaque-cookie-matching",
            },
            account_profile_key=profile_key,
        )

        self.assertEqual("success", outcome)
        self.assertEqual(profile_key, commit_reauth.await_args.kwargs["account_profile_key"])
        commit_reauth.assert_awaited_once()
        manager.update_token.assert_not_awaited()
        manager._mark_auth_success.assert_not_awaited()
        manager.enable_token.assert_not_awaited()
        manager._mark_auth_failure.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
