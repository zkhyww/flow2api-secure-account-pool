import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin
from src.core.config import config


class _TokenManager:
    def __init__(self, tokens):
        self._tokens = tokens
        self.db = None

    async def get_active_tokens(self):
        return list(self._tokens)


class ExtensionConnectionStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_manager = admin.token_manager
        self.original_mode = config.captcha_method
        self.tokens = [
            SimpleNamespace(id=1, image_enabled=True, credits=10, email="private-a"),
            SimpleNamespace(id=2, image_enabled=True, credits=10, email="private-b"),
        ]
        admin.token_manager = _TokenManager(self.tokens)
        config.set_captcha_method("extension")
        admin.active_admin_tokens.add("status-admin-session-fixture")
        app = FastAPI()
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        admin.active_admin_tokens.discard("status-admin-session-fixture")
        admin.token_manager = self.original_manager
        config.set_captcha_method(self.original_mode)

    async def test_status_is_whitelisted_and_reports_disconnected_extension(self):
        service = SimpleNamespace(
            active_connections=[],
            has_connection_for_token=AsyncMock(return_value=(False, "private-route")),
        )
        with patch(
            "src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance",
            new=AsyncMock(return_value=service),
        ):
            response = await self.client.get(
                "/api/admin/extension-connection-status",
                headers={"Authorization": "Bearer " + "status-admin-session-fixture"},
            )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual(
            {
                "service_online",
                "configured_port",
                "captcha_mode",
                "extension_connected",
                "connection_count",
                "active_account_count",
                "connected_account_count",
                "ready_account_count",
                "status",
                "error_class",
            },
            set(payload),
        )
        self.assertEqual("disconnected", payload["status"])
        self.assertEqual("extension_not_connected", payload["error_class"])
        self.assertFalse(payload["extension_connected"])
        self.assertEqual(0, payload["connected_account_count"])
        self.assertNotIn("private", response.text)

    async def test_status_reports_ready_when_a_bound_active_account_is_connected(self):
        service = SimpleNamespace(
            active_connections=[SimpleNamespace(capability_marker="yingce-flow2api-worker-v1")],
            has_connection_for_token=AsyncMock(side_effect=[(True, "private-route"), (False, "private-route")]),
        )
        with patch(
            "src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance",
            new=AsyncMock(return_value=service),
        ):
            response = await self.client.get(
                "/api/admin/extension-connection-status",
                headers={"Authorization": "Bearer " + "status-admin-session-fixture"},
            )

        payload = response.json()
        self.assertEqual("ready", payload["status"])
        self.assertIsNone(payload["error_class"])
        self.assertTrue(payload["extension_connected"])
        self.assertEqual(1, payload["connection_count"])
        self.assertEqual(2, payload["active_account_count"])
        self.assertEqual(1, payload["connected_account_count"])
        self.assertEqual(1, payload["ready_account_count"])

    async def test_status_names_connected_but_unbound_state_without_calling_it_connecting(self):
        service = SimpleNamespace(
            active_connections=[SimpleNamespace(capability_marker="yingce-flow2api-worker-v1")],
            has_connection_for_token=AsyncMock(return_value=(False, "private-route")),
        )
        with patch(
            "src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance",
            new=AsyncMock(return_value=service),
        ):
            response = await self.client.get(
                "/api/admin/extension-connection-status",
                headers={"Authorization": "Bearer " + "status-admin-session-fixture"},
            )

        payload = response.json()
        self.assertEqual("account_binding_required", payload["status"])
        self.assertEqual("extension_account_binding_pending", payload["error_class"])
        self.assertEqual(1, payload["connection_count"])
        self.assertEqual(0, payload["ready_account_count"])


if __name__ == "__main__":
    unittest.main()
