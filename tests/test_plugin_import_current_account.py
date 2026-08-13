import unittest
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from src.api import admin
from src.core.config import config


class _FakeFlowClient:
    async def st_to_at(self, session_token):
        return {
            "access_token": "access-fixture",
            "expires": "2030-01-01T00:00:00Z",
            "user": {"email": "user@example.test", "name": "Test User"},
        }


class _FakeDatabase:
    async def get_plugin_config(self):
        return SimpleNamespace(auto_enable_on_update=True)

    async def get_token_by_email(self, email):
        return None


class _FakeTokenManager:
    def __init__(self):
        self.flow_client = _FakeFlowClient()
        self.added = None

    async def add_token(self, **kwargs):
        self.added = kwargs
        return SimpleNamespace(id=17, email="user@example.test")


class PluginImportCurrentAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_api_key = config.api_key
        config.api_key = "api-key-fixture"
        self.original_db = admin.db
        self.original_token_manager = admin.token_manager
        self.fake_db = _FakeDatabase()
        self.fake_token_manager = _FakeTokenManager()
        admin.db = self.fake_db
        admin.token_manager = self.fake_token_manager

        app = FastAPI()
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        admin.db = self.original_db
        admin.token_manager = self.original_token_manager
        config.api_key = self.original_api_key

    async def test_api_key_import_adds_protocol_account_with_extension_route(self):
        response = await self.client.post(
            "/api/plugin/import-current-account",
            headers={"Authorization": "Bearer api-key-fixture"},
            json={
                "session_token": "session-fixture",
                "google_cookies": '[{"name":"SID","value":"cookie-fixture"}]',
                "extension_route_key": "flow-test-route",
                "refresh_interval_minutes": 30,
            },
        )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertIs(True, payload["success"])
        self.assertEqual("added", payload["action"])
        self.assertEqual(1, payload.get("added"))
        self.assertEqual(0, payload.get("updated"))
        self.assertEqual("user@example.test", payload["email"])
        self.assertEqual(17, payload["token_id"])
        self.assertEqual("protocol", self.fake_token_manager.added["protocol_mode"])
        self.assertEqual("flow-test-route", self.fake_token_manager.added["extension_route_key"])
        self.assertEqual(30, self.fake_token_manager.added["refresh_interval_minutes"])


if __name__ == "__main__":
    unittest.main()
