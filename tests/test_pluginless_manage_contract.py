import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin
from src.core.config import config


class _TokenManager:
    def __init__(self, tokens):
        self._tokens = list(tokens)
        self.db = None

    async def get_active_tokens(self):
        return list(self._tokens)


class PluginlessReadinessApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_manager = admin.token_manager
        self.original_mode = config.captcha_method
        self.admin_token = "pluginless-readiness-admin"
        admin.active_admin_tokens.add(self.admin_token)
        config.set_captcha_method("personal")
        app = FastAPI()
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        admin.active_admin_tokens.discard(self.admin_token)
        admin.token_manager = self.original_manager
        config.set_captcha_method(self.original_mode)

    async def test_personal_mode_reports_on_demand_readiness_without_extension_service(self):
        admin.token_manager = _TokenManager(
            [
                SimpleNamespace(id=1),
                SimpleNamespace(id=2),
                SimpleNamespace(id=3),
            ]
        )

        with patch(
            "src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance",
            new=AsyncMock(side_effect=AssertionError("extension service is not required")),
        ) as extension_service:
            response = await self.client.get(
                "/api/admin/extension-connection-status"
            )

        payload = response.json()
        self.assertEqual(200, response.status_code)
        self.assertEqual(8000, payload["configured_port"])
        self.assertEqual("personal", payload["captcha_mode"])
        self.assertEqual("ready", payload["status"])
        self.assertIsNone(payload["error_class"])
        self.assertEqual(3, payload["active_account_count"])
        self.assertEqual(3, payload["ready_account_count"])
        self.assertFalse(payload["extension_connected"])
        extension_service.assert_not_awaited()
        self.assertNotIn("extension_not_connected", response.text)
        self.assertNotIn("pair", response.text.lower())

    async def test_personal_mode_without_accounts_requests_an_account_not_a_plugin(self):
        admin.token_manager = _TokenManager([])

        response = await self.client.get(
            "/api/admin/extension-connection-status"
        )

        payload = response.json()
        self.assertEqual("account_required", payload["status"])
        self.assertEqual("account_not_configured", payload["error_class"])
        self.assertEqual(0, payload["ready_account_count"])
        self.assertNotIn("extension", payload["error_class"])
        self.assertNotIn("pair", response.text.lower())


class PluginlessManageStaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manage = Path("static/manage.html").read_text(encoding="utf-8")
        cls.test_page = Path("static/test.html").read_text(encoding="utf-8")

    def test_onboarding_copy_says_login_is_persisted_across_restarts(self):
        self.assertIn('id="browserAccountOnboardingHint"', self.manage)
        self.assertRegex(
            self.manage,
            r"登录一次[^<]{0,80}(?:服务|Windows)[^<]{0,80}重启[^<]{0,80}自动恢复",
        )

    def test_onboarding_button_is_clearly_for_adding_a_new_account(self):
        self.assertIn("添加新 Google 账号", self.manage)
        self.assertIn("已有账号无需重复登录", self.manage)

    def test_extension_controls_are_only_in_explicit_advanced_fallback(self):
        details = re.search(
            r"<details[^>]*id=[\"']advancedExtensionFallback[\"'][^>]*>(?P<body>.*?)</details>",
            self.manage,
            re.DOTALL,
        )
        self.assertIsNotNone(details)
        self.assertIn("高级备用", details.group("body"))
        self.assertIn('id="extensionConnectBtn"', details.group("body"))
        self.assertNotIn(
            'id="extensionConnectBtn"',
            self.manage[: details.start()],
        )

    def test_manage_status_branches_on_personal_before_extension_copy(self):
        self.assertIn("state.captcha_mode==='personal'", self.manage)
        self.assertIn("内置浏览器按需", self.manage)
        self.assertIn("window.currentCaptchaMode==='extension'", self.manage)

    def test_test_page_hides_extension_state_outside_extension_mode(self):
        self.assertRegex(
            self.test_page,
            r'id="extensionConnectionStatus"[^>]*class="[^"]*hidden',
        )
        self.assertIn('state.captcha_mode === "personal"', self.test_page)
        self.assertIn("内置浏览器按需", self.test_page)


if __name__ == "__main__":
    unittest.main()
