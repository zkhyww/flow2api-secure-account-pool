import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin
from src.core.database import Database
from src.core.models import RequestLog, Task, Token
from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    ResidentTabInfo,
    _PersonalBrowserPoolService,
)
from src.services.concurrency_manager import ConcurrencyManager
from src.services import protocol_login
from src.services.token_manager import TokenManager


ACCOUNT_PUBLIC_FIELDS = {
    "id",
    "display_name",
    "is_active",
    "auth_status",
    "status",
    "error_class",
    "credits",
    "credits_reserved",
    "credits_available",
    "image_learned_limit",
    "image_inflight",
    "image_cooldown_reason",
    "image_cooldown_remaining_seconds",
    "video_learned_limit",
    "video_inflight",
    "video_cooldown_reason",
    "video_cooldown_remaining_seconds",
    "browser_in_use",
    "browser_worker_index",
    "browser_reserved_slots",
    "browser_resident_slots",
}

BROWSER_OVERVIEW_FIELDS = {
    "status",
    "error_class",
    "configured_workers",
    "max_workers",
    "created_workers",
    "live_workers",
    "total_reservations",
    "total_inflight",
    "total_capacity",
    "occupied_slots",
}

EDIT_CONFIG_PUBLIC_FIELDS = {
    "id",
    "remark",
    "project_id",
    "project_name",
    "image_enabled",
    "video_enabled",
    "image_concurrency",
    "video_concurrency",
    "protocol_mode",
    "auto_refresh_enabled",
    "refresh_interval_minutes",
}


class _FakeBrowserProcess:
    def __init__(self):
        self.stopped = False
        self.targets = []


class Batch5AdminObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "batch5-observability.db"))
        await self.db.init_db()

        self.sentinels = {
            "st": "B5_PRIVATE_SESSION_VALUE",
            "at": "B5_PRIVATE_ACCESS_VALUE",
            "cookies": "B5_PRIVATE_COOKIE_VALUE",
            "password": "B5_PRIVATE_PASSWORD_VALUE",
            "captcha_proxy": "B5_PRIVATE_CAPTCHA_PROXY_VALUE",
            "proxy": "B5_PRIVATE_PROXY_VALUE",
            "prompt": "B5_PRIVATE_PROMPT_VALUE",
            "media": "B5_PRIVATE_MEDIA_VALUE",
            "response": "B5_PRIVATE_RESPONSE_VALUE",
            "captcha_token": "B5_PRIVATE_CAPTCHA_TOKEN_VALUE",
        }
        self.token_id = await self.db.add_token(
            Token(
                st="placeholder",
                at=None,
                email="",
                name="account-alpha",
                is_active=False,
                credits=7,
                image_concurrency=5,
                video_concurrency=4,
            )
        )
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                """
                UPDATE tokens
                SET st = ?, at = ?, google_cookies = ?, login_password = ?,
                    captcha_proxy_url = ?, proxy_url = ?, ban_reason = ?
                WHERE id = ?
                """,
                (
                    self.sentinels["st"],
                    self.sentinels["at"],
                    self.sentinels["cookies"],
                    self.sentinels["password"],
                    self.sentinels["captcha_proxy"],
                    self.sentinels["proxy"],
                    "authentication",
                    self.token_id,
                ),
            )
            await conn.commit()

        await self.db.create_task(
            Task(
                task_id="batch5-reserved-task",
                token_id=self.token_id,
                model="test-model",
                prompt=self.sentinels["prompt"],
                status="accepted",
                result_urls=[self.sentinels["media"]],
                quota_state="reserved",
                quota_reserved=2,
            )
        )
        await self.db.add_request_log(
            RequestLog(
                token_id=self.token_id,
                operation="batch5-observability",
                request_body=json.dumps({"captcha_token": self.sentinels["captcha_token"]}),
                response_body=self.sentinels["response"],
                status_code=500,
                duration=0.01,
            )
        )

        self.clock_value = 1000.0
        self.concurrency = ConcurrencyManager(clock=lambda: self.clock_value)
        await self.concurrency.initialize(
            [
                Token(
                    id=self.token_id,
                    st="placeholder",
                    email="",
                    credits=7,
                    image_concurrency=5,
                    video_concurrency=4,
                )
            ]
        )
        await self.concurrency.record_success(self.token_id, "image")
        await self.concurrency.acquire_image(self.token_id)
        await self.concurrency.acquire_video(self.token_id)
        await self.concurrency.record_rate_limit(
            self.token_id,
            "video",
            cooldown_seconds=90,
        )

        self.pool = _PersonalBrowserPoolService(self.db)
        worker = BrowserCaptchaService(
            db=self.db,
            browser_instance_id=1,
            max_resident_tabs_override=2,
        )
        worker._initialized = True
        worker.browser = _FakeBrowserProcess()
        resident = ResidentTabInfo(
            tab=object(),
            slot_id="b1-observe-slot",
            project_id="project-alpha",
            token_id=self.token_id,
        )
        resident.pending_assignment_count = 1
        worker._resident_tabs[resident.slot_id] = resident
        worker._token_resident_affinity[str(self.token_id)] = resident.slot_id
        worker._project_resident_affinity[resident.project_id] = resident.slot_id
        self.pool._workers = [worker]
        self.pool._worker_tab_limits = [2]
        self.pool._worker_dispatch_reservations = {0: 1}
        self.pool._token_worker_affinity[str(self.token_id)] = 0
        self.pool._project_worker_affinity[resident.project_id] = 0

        self.original_db = admin.db
        self.original_concurrency = admin.concurrency_manager
        admin.db = self.db
        admin.concurrency_manager = self.concurrency

        self.admin_token = "batch5-admin-session"
        admin.active_admin_tokens.add(self.admin_token)
        app = FastAPI()
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )

        self.get_instance_patch = patch.object(
            BrowserCaptchaService,
            "get_instance",
            new=AsyncMock(return_value=self.pool),
        )
        self.initialize_patch = patch.object(
            BrowserCaptchaService,
            "initialize",
            new=AsyncMock(),
        )
        self.resolve_count_patch = patch.object(
            BrowserCaptchaService,
            "_resolve_configured_browser_count",
            return_value=2,
        )
        self.get_instance = self.get_instance_patch.start()
        self.initialize = self.initialize_patch.start()
        self.resolve_count_patch.start()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.resolve_count_patch.stop()
        self.initialize_patch.stop()
        self.get_instance_patch.stop()
        admin.active_admin_tokens.discard(self.admin_token)
        admin.db = self.original_db
        admin.concurrency_manager = self.original_concurrency
        for worker in self.pool._workers:
            worker.browser = None
            worker._initialized = False
            worker._resident_tabs.clear()
        self.temp_dir.cleanup()

    async def _request_page(self):
        response = await self.client.get("/api/tokens?page=1&page_size=25")
        self.assertEqual(200, response.status_code)
        return response

    async def test_concurrency_manager_exposes_one_lock_safe_snapshot_for_many_accounts(self):
        self.assertTrue(
            hasattr(self.concurrency, "get_observability_snapshot"),
            "ConcurrencyManager lacks the Batch 5 bulk observability snapshot",
        )

        snapshot = await self.concurrency.get_observability_snapshot(
            [self.token_id, 999999]
        )

        current = snapshot[self.token_id]
        self.assertEqual(4, current["image_learned_limit"])
        self.assertEqual(1, current["image_inflight"])
        self.assertIsNone(current["image_cooldown_reason"])
        self.assertEqual(0, current["image_cooldown_remaining_seconds"])
        self.assertEqual(1, current["video_learned_limit"])
        self.assertEqual(1, current["video_inflight"])
        self.assertEqual("429_rate_limit", current["video_cooldown_reason"])
        self.assertEqual(90, current["video_cooldown_remaining_seconds"])

        unknown = snapshot[999999]
        self.assertEqual(0, unknown["image_inflight"])
        self.assertEqual(0, unknown["video_inflight"])

    async def test_personal_pool_snapshot_reports_capacity_without_starting_a_browser(self):
        self.assertTrue(
            hasattr(self.pool, "get_observability_snapshot"),
            "personal browser pool lacks the Batch 5 read-only snapshot",
        )

        snapshot = await self.pool.get_observability_snapshot(
            [self.token_id, 999999]
        )

        self.assertEqual(
            {
                "status": "ok",
                "error_class": None,
                "configured_workers": 2,
                "max_workers": 10,
                "created_workers": 1,
                "live_workers": 1,
                "total_reservations": 1,
                "total_inflight": 1,
                "total_capacity": 2,
                "occupied_slots": 1,
            },
            snapshot["overview"],
        )
        self.assertEqual(
            {
                "browser_in_use": True,
                "browser_worker_index": 1,
                "browser_reserved_slots": 1,
                "browser_resident_slots": 1,
            },
            snapshot["accounts"][self.token_id],
        )
        self.assertEqual(0, snapshot["accounts"][999999]["browser_reserved_slots"])
        self.assertFalse(snapshot["accounts"][999999]["browser_in_use"])
        self.initialize.assert_not_awaited()

    async def test_paginated_admin_payload_is_strictly_whitelisted_and_omits_secrets(self):
        response = await self._request_page()

        for label, sentinel in self.sentinels.items():
            with self.subTest(secret_class=label):
                self.assertNotIn(sentinel, response.text)

        payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else payload
        self.assertEqual(1, len(items))
        self.assertEqual(ACCOUNT_PUBLIC_FIELDS, set(items[0]))
        self.assertFalse(
            {
                "st",
                "at",
                "token",
                "google_cookies",
                "login_password",
                "captcha_token",
                "captcha_proxy_url",
                "proxy_url",
                "prompt",
                "media_url",
                "response_body",
            }
            & set(items[0])
        )

    async def test_legacy_direct_list_keeps_shape_but_uses_the_same_safe_public_fields(self):
        payload = await admin.get_tokens(token="fixture")

        self.assertIsInstance(payload, list, "frozen direct callers still require a list")
        self.assertEqual(1, len(payload))
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        for label, sentinel in self.sentinels.items():
            with self.subTest(secret_class=label):
                self.assertNotIn(sentinel, serialized)

        self.assertEqual(ACCOUNT_PUBLIC_FIELDS, set(payload[0]))
        self.assertFalse(
            {
                "st",
                "at",
                "token",
                "google_cookies",
                "login_password",
                "captcha_token",
                "captcha_proxy_url",
                "proxy_url",
                "prompt",
                "media_url",
                "response_body",
            }
            & set(payload[0])
        )

    async def test_admin_page_combines_quota_concurrency_and_browser_fact_sources(self):
        response = await self._request_page()
        payload = response.json()

        self.assertIsInstance(payload, dict, "explicit pagination must return an envelope")
        self.assertEqual(
            {"items", "total", "page", "page_size", "has_next", "browser"},
            set(payload),
        )
        self.assertEqual(1, payload["total"])
        self.assertEqual(1, payload["page"])
        self.assertEqual(25, payload["page_size"])
        self.assertFalse(payload["has_next"])

        item = payload["items"][0]
        self.assertEqual(self.token_id, item["id"])
        self.assertEqual("account-alpha", item["display_name"])
        self.assertFalse(item["is_active"])
        self.assertEqual("authentication_failed", item["auth_status"])
        self.assertEqual("ok", item["status"])
        self.assertIsNone(item["error_class"])
        self.assertEqual(7, item["credits"])
        self.assertEqual(2, item["credits_reserved"])
        self.assertEqual(5, item["credits_available"])
        self.assertEqual(4, item["image_learned_limit"])
        self.assertEqual(1, item["image_inflight"])
        self.assertEqual(1, item["video_learned_limit"])
        self.assertEqual(1, item["video_inflight"])
        self.assertEqual("429_rate_limit", item["video_cooldown_reason"])
        self.assertEqual(90, item["video_cooldown_remaining_seconds"])
        self.assertTrue(item["browser_in_use"])
        self.assertEqual(1, item["browser_worker_index"])
        self.assertEqual(1, item["browser_reserved_slots"])
        self.assertEqual(1, item["browser_resident_slots"])

    async def test_admin_browser_overview_is_read_only_and_bounded_by_ten(self):
        response = await self._request_page()
        payload = response.json()

        self.assertIsInstance(payload, dict, "browser overview belongs to the paginated envelope")
        overview = payload["browser"]
        self.assertEqual(BROWSER_OVERVIEW_FIELDS, set(overview))
        self.assertEqual(2, overview["configured_workers"])
        self.assertEqual(10, overview["max_workers"])
        self.assertEqual(1, overview["created_workers"])
        self.assertEqual(1, overview["live_workers"])
        self.assertEqual(1, overview["total_reservations"])
        self.assertEqual(1, overview["total_inflight"])
        self.assertEqual(2, overview["total_capacity"])
        self.assertEqual(1, overview["occupied_slots"])
        self.initialize.assert_not_awaited()

    async def test_edit_config_returns_only_noncredential_fields(self):
        response = await self.client.get(f"/api/tokens/{self.token_id}/edit-config")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual(EDIT_CONFIG_PUBLIC_FIELDS, set(payload))
        for label, sentinel in self.sentinels.items():
            with self.subTest(secret_class=label):
                self.assertNotIn(sentinel, response.text)

    async def test_noncredential_update_preserves_write_only_credentials_without_external_calls(self):
        async def raw_credentials():
            async with self.db._connect() as conn:
                cursor = await conn.execute(
                    """
                    SELECT st, at, google_cookies, captcha_proxy_url, proxy_url
                    FROM tokens
                    WHERE id = ?
                    """,
                    (self.token_id,),
                )
                row = await cursor.fetchone()
                return tuple(row)

        before = await raw_credentials()
        flow_client = AsyncMock()
        manager = TokenManager(self.db, flow_client)

        with patch.object(admin, "token_manager", manager), patch.object(
            protocol_login.protocol_loginer,
            "login",
            new=AsyncMock(),
        ) as protocol_login_call:
            response = await self.client.put(
                f"/api/tokens/{self.token_id}",
                json={"remark": "updated-public-remark"},
            )

        after = await raw_credentials()
        self.assertEqual(200, response.status_code)
        self.assertTrue(before == after, "omitted write-only fields must remain unchanged")
        updated = await self.db.get_token_edit_config(self.token_id)
        self.assertEqual("updated-public-remark", updated["remark"])
        flow_client.st_to_at.assert_not_awaited()
        protocol_login_call.assert_not_awaited()
        self.get_instance.assert_not_awaited()
        self.initialize.assert_not_awaited()

    async def test_update_failures_return_public_error_and_log_only_exception_type(self):
        cases = (
            ("database_update", False, RuntimeError),
            ("st_conversion", True, ValueError),
        )

        for label, include_st, exception_type in cases:
            with self.subTest(failure_source=label):
                private_exception = f"B5_PRIVATE_{label.upper()}_EXCEPTION_VALUE"
                flow_client = AsyncMock()
                manager = TokenManager(self.db, flow_client)
                if include_st:
                    flow_client.st_to_at.side_effect = exception_type(private_exception)
                else:
                    manager.update_token = AsyncMock(
                        side_effect=exception_type(private_exception)
                    )
                payload = {"remark": "public-update"}
                if include_st:
                    payload["st"] = "write-only-fixture"

                with patch.object(admin, "token_manager", manager), patch(
                    "src.core.logger.debug_logger.log_error"
                ) as log_error, patch.object(
                    protocol_login.protocol_loginer,
                    "login",
                    new=AsyncMock(),
                ) as protocol_login_call:
                    response = await self.client.put(
                        f"/api/tokens/{self.token_id}",
                        json=payload,
                    )

                self.assertEqual(500, response.status_code)
                self.assertEqual("Token update failed", response.json().get("detail"))
                self.assertNotIn(private_exception, response.text)
                serialized_logs = " ".join(
                    str(argument)
                    for call in log_error.call_args_list
                    for argument in call.args
                )
                self.assertIn(exception_type.__name__, serialized_logs)
                self.assertNotIn(private_exception, serialized_logs)
                protocol_login_call.assert_not_awaited()
                self.get_instance.assert_not_awaited()
                self.initialize.assert_not_awaited()

    async def test_status_provider_failures_degrade_to_public_classes_without_exception_text(self):
        private_exception = "B5_PRIVATE_STATUS_EXCEPTION_VALUE"
        broken_concurrency = AsyncMock(side_effect=RuntimeError(private_exception))
        broken_browser = AsyncMock(side_effect=RuntimeError(private_exception))

        with patch.object(
            self.concurrency,
            "get_observability_snapshot",
            new=broken_concurrency,
            create=True,
        ), patch.object(
            self.pool,
            "get_observability_snapshot",
            new=broken_browser,
            create=True,
        ):
            response = await self._request_page()

        self.assertNotIn(private_exception, response.text)
        payload = response.json()
        self.assertIsInstance(payload, dict, "status failures must preserve the page contract")
        item = payload["items"][0]
        self.assertEqual("unknown", item["status"])
        self.assertEqual("status_unavailable", item["error_class"])
        self.assertEqual(0, item["image_inflight"])
        self.assertEqual(0, item["video_inflight"])
        self.assertEqual("unknown", payload["browser"]["status"])
        self.assertEqual("status_unavailable", payload["browser"]["error_class"])
        self.initialize.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
