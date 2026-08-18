import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin, routes
from src.core.config import config
from src.services.admin_test_capability import AdminTestCapabilityService


class AdminTestCapabilityServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_expires_and_only_stores_digests(self):
        now = 100.0
        service = AdminTestCapabilityService(ttl_seconds=10, clock=lambda: now)
        capability = await service.issue("admin-session-fixture")
        self.assertTrue(await service.verify(capability, {"admin-session-fixture"}))
        self.assertNotIn(capability, repr(service._records))

        now = 111.0
        self.assertFalse(await service.verify(capability, {"admin-session-fixture"}))

    async def test_logout_invalidates_bound_capability_immediately(self):
        service = AdminTestCapabilityService(ttl_seconds=60)
        capability = await service.issue("admin-session-fixture")
        self.assertTrue(await service.verify(capability, {"admin-session-fixture"}))
        self.assertFalse(await service.verify(capability, set()))


class AdminTestCapabilityApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_api_key = config.api_key
        config.api_key = "manual-api-key-fixture"
        admin.active_admin_tokens.add("admin-session-fixture")
        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        admin.active_admin_tokens.discard("admin-session-fixture")
        config.api_key = self.original_api_key
        await self.client.aclose()

    async def _issue(self):
        response = await self.client.post(
            "/api/admin/test-capability",
            headers={"Authorization": "Bearer admin-session-fixture"},
        )
        self.assertEqual(200, response.status_code)
        return response.json()["capability"]

    async def test_capability_is_accepted_only_by_test_endpoints(self):
        capability = await self._issue()
        headers = {"X-Flow2API-Test-Capability": capability}

        test_models = await self.client.get("/api/test/models", headers=headers)
        self.assertEqual(200, test_models.status_code)
        public_models = await self.client.get("/v1/models", headers=headers)
        self.assertEqual(401, public_models.status_code)

        manual_fallback = await self.client.get(
            "/v1/models",
            headers={"Authorization": "Bearer manual-api-key-fixture"},
        )
        self.assertEqual(200, manual_fallback.status_code)

    async def test_validated_omni_ten_seconds_is_public_and_no_hidden_mapping_is_exposed(self):
        capability = await self._issue()
        test_headers = {"X-Flow2API-Test-Capability": capability}
        public_headers = {"Authorization": "Bearer manual-api-key-fixture"}

        test_catalog_response = await self.client.get(
            "/api/test/models",
            headers=test_headers,
        )
        public_catalog_response = await self.client.get(
            "/v1/models",
            headers=public_headers,
        )
        self.assertEqual(200, test_catalog_response.status_code)
        self.assertEqual(200, public_catalog_response.status_code)

        for response in (test_catalog_response, public_catalog_response):
            omni = next(
                entry
                for entry in response.json()["data"]
                if entry["capability_id"] == "omni-flash"
            )
            self.assertEqual(
                {"8", "10"},
                {
                    str(option["value"])
                    for option in omni["options"]["duration_seconds"]
                },
            )
            self.assertFalse(
                any(
                    item.get("validation_status") == "hidden"
                    for item in omni["compatibility_map"]
                )
            )

        body = {
            "model": "omni_10s",
            "messages": [{"role": "user", "content": "fixture"}],
            "stream": False,
        }
        with patch.object(
            routes,
            "_collect_non_stream_result",
            new=AsyncMock(return_value='{"content":"ok"}'),
        ) as collect:
            test_generation = await self.client.post(
                "/api/test/chat/completions",
                headers=test_headers,
                json=body,
            )
            public_generation = await self.client.post(
                "/v1/chat/completions",
                headers=public_headers,
                json=body,
            )

        self.assertEqual(200, test_generation.status_code)
        self.assertEqual(200, public_generation.status_code)
        self.assertEqual(2, collect.await_count)
        self.assertTrue(
            all(call.args[0] == "omni_10s" for call in collect.await_args_list)
        )

    async def test_logout_invalidates_issued_capability(self):
        capability = await self._issue()
        admin.active_admin_tokens.discard("admin-session-fixture")
        response = await self.client.get(
            "/api/test/models",
            headers={"X-Flow2API-Test-Capability": capability},
        )
        self.assertEqual(401, response.status_code)

    async def test_diagnostic_account_selection_requires_capability_and_reaches_test_generation_only(self):
        body = {
            "model": "gemini-3.1-flash-image",
            "messages": [{"role": "user", "content": "fixture"}],
            "stream": False,
            "diagnostic_token_id": 23,
        }
        denied = await self.client.post("/api/test/chat/completions", json=body)
        self.assertEqual(401, denied.status_code)

        capability = await self._issue()
        with patch.object(
            routes,
            "_collect_non_stream_result",
            new=AsyncMock(return_value='{"content":"ok"}'),
        ) as collect:
            allowed = await self.client.post(
                "/api/test/chat/completions",
                headers={"X-Flow2API-Test-Capability": capability},
                json=body,
            )
        self.assertEqual(200, allowed.status_code)
        self.assertEqual(23, collect.await_args.kwargs["diagnostic_token_id"])

    async def test_streaming_test_generation_accepts_capability_and_forwards_diagnostic_account(self):
        class _StreamingHandler:
            def __init__(self):
                self.kwargs = None

            async def handle_generation(self, **kwargs):
                self.kwargs = kwargs
                yield 'data: {"choices":[{"index":0,"delta":{"reasoning_content":"progress"},"finish_reason":null}]}\n\n'

        capability = await self._issue()
        handler = _StreamingHandler()
        body = {
            "model": "gemini-3.1-flash-image",
            "messages": [{"role": "user", "content": "fixture"}],
            "stream": True,
            "diagnostic_token_id": 23,
        }
        with patch.object(routes, "_ensure_generation_handler", return_value=handler):
            response = await self.client.post(
                "/api/test/chat/completions",
                headers={"X-Flow2API-Test-Capability": capability},
                json=body,
            )

        self.assertEqual(200, response.status_code)
        self.assertIn("text/event-stream", response.headers.get("content-type", ""))
        self.assertIn("progress", response.text)
        self.assertIn("[DONE]", response.text)
        self.assertTrue(handler.kwargs["stream"])
        self.assertEqual(23, handler.kwargs["diagnostic_token_id"])

    async def test_model_availability_requires_capability_validates_account_and_returns_only_allowlisted_fields(self):
        class _AvailabilityDatabase:
            async def get_token(self, token_id):
                return object() if token_id == 23 else None

            async def get_account_model_availability(self, token_id):
                self.requested_token_id = token_id
                return {
                    "image-model": {
                        "status": "available",
                        "error_class": "",
                        "last_verified_at": "2026-08-13 12:00:00",
                        "successful_generations": 2,
                    }
                }

        database = _AvailabilityDatabase()
        handler = type("_Handler", (), {"db": database})()

        denied = await self.client.get("/api/test/model-availability?diagnostic_token_id=23")
        self.assertEqual(401, denied.status_code)

        capability = await self._issue()
        with patch.object(routes, "_ensure_generation_handler", return_value=handler):
            missing = await self.client.get(
                "/api/test/model-availability?diagnostic_token_id=999",
                headers={"X-Flow2API-Test-Capability": capability},
            )
            allowed = await self.client.get(
                "/api/test/model-availability?diagnostic_token_id=23",
                headers={"X-Flow2API-Test-Capability": capability},
            )

        self.assertEqual(404, missing.status_code)
        self.assertEqual(200, allowed.status_code)
        self.assertEqual(
            {
                "items": [
                    {
                        "model": "image-model",
                        "status": "available",
                        "error_class": "",
                        "last_verified_at": "2026-08-13 12:00:00",
                    }
                ]
            },
            allowed.json(),
        )

    async def test_diagnostic_account_summaries_use_a_strict_public_allowlist(self):
        class _SummaryDatabase:
            async def get_tokens_page_with_stats(self, *, limit, offset):
                self.request = (limit, offset)
                return {
                    "items": [
                        {
                            "id": 23,
                            "name": "Primary",
                            "remark": "not-returned",
                            "is_active": True,
                            "auth_state": "ok",
                            "has_account_profile": True,
                            "credits": 7,
                            "image_concurrency": 2,
                            "browser_in_use": True,
                        },
                        {
                            "id": 24,
                            "name": "",
                            "remark": "Backup",
                            "is_active": False,
                            "auth_state": "reauth_required",
                            "has_account_profile": False,
                            "credits": 99,
                        },
                    ],
                    "total": 2,
                }

        original_db = admin.db
        summary_db = _SummaryDatabase()
        admin.db = summary_db
        try:
            denied = await self.client.get("/api/admin/test-accounts")
            self.assertEqual(401, denied.status_code)

            response = await self.client.get(
                "/api/admin/test-accounts",
                headers={"Authorization": "Bearer admin-session-fixture"},
            )
        finally:
            admin.db = original_db

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual({"items"}, set(payload))
        self.assertEqual(
            [
                {"id": 23, "display_name": "Primary", "auth_status": "正常"},
                {"id": 24, "display_name": "Backup", "auth_status": "已停用"},
            ],
            payload["items"],
        )
        self.assertTrue(all(set(item) == {"id", "display_name", "auth_status"} for item in payload["items"]))


if __name__ == "__main__":
    unittest.main()
