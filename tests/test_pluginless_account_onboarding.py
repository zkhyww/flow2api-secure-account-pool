import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api import admin
from src.services.browser_captcha_personal import BrowserCaptchaService


class PluginlessBrowserCaptureTests(unittest.IsolatedAsyncioTestCase):
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
        )
        database = SimpleNamespace(
            get_token_by_email=AsyncMock(return_value=None),
        )
        admin.token_manager = manager
        admin.db = database
        private_result = {
            "st": "opaque-session-native",
            "google_cookies": "SID=opaque-cookie-native",
        }

        outcome = await admin._persist_native_onboarding_result(private_result)

        self.assertEqual("success", outcome)
        manager.add_token.assert_awaited_once_with(
            st="opaque-session-native",
            remark="Added from native browser login",
            protocol_mode="protocol",
            google_cookies="SID=opaque-cookie-native",
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
        )
        database = SimpleNamespace(
            get_token_by_email=AsyncMock(return_value=existing),
        )
        admin.token_manager = manager
        admin.db = database

        outcome = await admin._persist_native_onboarding_result(
            {
                "st": "opaque-session-updated",
                "google_cookies": "SID=opaque-cookie-updated",
            }
        )

        self.assertEqual("updated", outcome)
        update_kwargs = manager.update_token.await_args.kwargs
        self.assertEqual(7, update_kwargs["token_id"])
        self.assertEqual("opaque-session-updated", update_kwargs["st"])
        self.assertEqual("opaque-access-native", update_kwargs["at"])
        self.assertEqual("protocol", update_kwargs["protocol_mode"])
        self.assertEqual(
            "SID=opaque-cookie-updated",
            update_kwargs["google_cookies"],
        )
        manager.disable_token.assert_awaited_once_with(7)
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
                {"google_cookies": "SID=opaque-cookie-only"}
            )

        admin.token_manager.flow_client.st_to_at.assert_not_awaited()
        admin.token_manager.add_token.assert_not_awaited()
        admin.token_manager.update_token.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
