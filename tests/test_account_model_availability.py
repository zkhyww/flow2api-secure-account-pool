import asyncio
import inspect
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, patch

from src.core.account_tiers import PAYGATE_TIER_NOT_PAID
from src.core.config import config
from src.core.database import Database
from src.core.models import Project, Task, Token
from src.services.concurrency_manager import ConcurrencyManager
from src.services.generation_handler import GenerationHandler
from src.services.load_balancer import LoadBalancer
from src.services.proxy_manager import ProxyManager
from src.services.token_manager import TokenManager


IMAGE_MODEL = "gemini-3.1-flash-image-landscape"
VIDEO_MODEL = "veo_3_1_t2v_fast_landscape"


class _AvailabilityFlowError(Exception):
    def __init__(self, error_code):
        super().__init__(error_code)
        status_by_code = {
            "PUBLIC_ERROR_MODEL_ACCESS_DENIED": 403,
            "PUBLIC_ERROR_MEMBERSHIP_TIER": 403,
            "QUOTA_EXHAUSTED": 402,
            "RATE_LIMITED": 429,
            "UPSTREAM_FAILURE": 503,
        }
        self.status_code = status_by_code.get(error_code, 502)
        self.error_code = error_code


class _AvailabilityFlowClient:
    def __init__(self):
        self.mode = "success"
        self.video_poll_mode = "success"
        self.image_submit_calls = 0
        self.video_submit_calls = 0

    def clear_request_fingerprint(self):
        return None

    async def prefill_remote_browser_pool(self, **kwargs):
        return None

    async def generate_image(self, **kwargs):
        self.image_submit_calls += 1
        if self.mode == "denied":
            raise _AvailabilityFlowError("PUBLIC_ERROR_MODEL_ACCESS_DENIED")
        if self.mode == "membership":
            raise _AvailabilityFlowError("PUBLIC_ERROR_MEMBERSHIP_TIER")
        if self.mode == "recaptcha":
            raise _AvailabilityFlowError("RECAPTCHA")
        if self.mode == "quota":
            raise _AvailabilityFlowError("QUOTA_EXHAUSTED")
        if self.mode == "rate_limit":
            raise _AvailabilityFlowError("RATE_LIMITED")
        if self.mode in {"temporary_failure", "upstream_5xx"}:
            raise _AvailabilityFlowError("UPSTREAM_FAILURE")
        return (
            {"media": [{"name": "fixture", "image": {"generatedImage": {"fifeUrl": "fixture-url"}}}]},
            "fixture-session",
            {},
        )

    async def generate_video_text(self, **kwargs):
        self.video_submit_calls += 1
        if self.mode == "denied":
            raise _AvailabilityFlowError("PUBLIC_ERROR_MODEL_ACCESS_DENIED")
        if self.mode == "membership":
            raise _AvailabilityFlowError("PUBLIC_ERROR_MEMBERSHIP_TIER")
        if self.mode == "recaptcha":
            raise _AvailabilityFlowError("RECAPTCHA")
        if self.mode == "quota":
            raise _AvailabilityFlowError("QUOTA_EXHAUSTED")
        if self.mode == "rate_limit":
            raise _AvailabilityFlowError("RATE_LIMITED")
        if self.mode in {"temporary_failure", "upstream_5xx"}:
            raise _AvailabilityFlowError("UPSTREAM_FAILURE")
        return {
            "operations": [
                {
                    "operation": {"name": f"availability-video-{self.video_submit_calls}"},
                    "sceneId": "availability-scene",
                }
            ]
        }

    async def check_video_status(self, _at, operations):
        operation_name = (operations[0].get("operation") or {}).get(
            "name", "availability-video"
        )
        if self.video_poll_mode in {"model_access_denied", "membership_tier"}:
            error_code = (
                "PUBLIC_ERROR_MODEL_ACCESS_DENIED"
                if self.video_poll_mode == "model_access_denied"
                else "PUBLIC_ERROR_MEMBERSHIP_TIER"
            )
            return {
                "operations": [
                    {
                        "status": "MEDIA_GENERATION_STATUS_FAILED",
                        "operation": {
                            "name": operation_name,
                            "error": {
                                "code": error_code,
                                "message": "fixture explicit denial",
                            },
                        },
                    }
                ]
            }
        return {
            "operations": [
                {
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
                    "mediaName": "availability-video-media",
                    "operation": {
                        "name": operation_name,
                        "metadata": {
                            "video": {
                                "mediaName": "availability-video-media",
                                "mediaGenerationId": "availability-video-media",
                                "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
                                "model": "availability-video-model",
                                "duration": 4,
                            }
                        },
                    },
                }
            ]
        }

    async def get_media_url_redirect(self, *_args, **_kwargs):
        return "https://flow-content.google/availability-video.mp4"


class AccountModelAvailabilityDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "availability.db")
        self.db = Database(self.db_path)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def _insert_tokens(self, connection, *token_ids):
        connection.executemany(
            "INSERT INTO tokens (id, st, email) VALUES (?, ?, ?)",
            [(token_id, f"fixture-st-{token_id}", f"fixture-{token_id}@example.test") for token_id in token_ids],
        )

    async def _build_generation_handler(self):
        flow = _AvailabilityFlowClient()
        token_id = await self.db.add_token(
            Token(
                st="generation-fixture",
                at=None,
                at_expires=datetime.now(timezone.utc) + timedelta(hours=2),
                email="",
                credits=999,
                user_paygate_tier=PAYGATE_TIER_NOT_PAID,
                image_concurrency=-1,
                video_concurrency=-1,
            )
        )
        await self.db.add_project(Project(project_id="generation-project", token_id=token_id, project_name="fixture"))
        await self.db.update_token(token_id, credits=999)
        token_manager = TokenManager(self.db, flow)
        token_manager._get_project_pool_size = lambda: 1
        token_manager._should_refresh_at = lambda token: False
        for token in await self.db.get_active_tokens():
            token_manager._mark_at_valid(token.id)
        concurrency_manager = ConcurrencyManager()
        await concurrency_manager.initialize(await self.db.get_active_tokens())
        return (
            GenerationHandler(
                flow,
                token_manager,
                LoadBalancer(token_manager, concurrency_manager),
                self.db,
                concurrency_manager,
                ProxyManager(self.db),
            ),
            flow,
            token_id,
        )

    async def _run_generation(
        self,
        handler,
        *,
        model,
        key=None,
        diagnostic_token_id=None,
    ):
        return [
            chunk
            async for chunk in handler.handle_generation(
                model=model,
                prompt="fixture",
                stream=False,
                idempotency_key=key,
                diagnostic_token_id=diagnostic_token_id,
            )
        ]

    async def _run_image_generation(self, handler, key):
        return await self._run_generation(handler, model=IMAGE_MODEL, key=key)

    async def _assert_video_poll_denial_recorded(self, *, error_class, key):
        old_cache_enabled = config.cache_enabled
        old_captcha_method = config.captcha_method
        config.set_cache_enabled(False)
        config.set_captcha_method("yescaptcha")
        try:
            await self.db.init_db()
            await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
            handler, flow, token_id = await self._build_generation_handler()
            flow.video_poll_mode = error_class

            await self._run_generation(
                handler,
                model=VIDEO_MODEL,
                key=key,
                diagnostic_token_id=token_id,
            )

            availability = await self.db.get_account_model_availability(token_id)
            self.assertEqual(1, flow.video_submit_calls)
            self.assertEqual("unavailable", availability[VIDEO_MODEL]["status"])
            self.assertEqual(error_class, availability[VIDEO_MODEL]["error_class"])
        finally:
            config.set_cache_enabled(old_cache_enabled)
            config.set_captcha_method(old_captcha_method)

    async def test_delete_token_removes_model_availability_and_existing_related_rows(self):
        await self.db.init_db()
        token_id = await self.db.add_token(
            Token(
                st="delete-fixture",
                at=None,
                email="delete-fixture@example.test",
                credits=1,
                user_paygate_tier=PAYGATE_TIER_NOT_PAID,
            )
        )
        await self.db.add_project(
            Project(
                project_id="delete-project",
                token_id=token_id,
                project_name="delete-fixture",
            )
        )
        await self.db.create_task(
            Task(
                task_id="delete-task",
                token_id=token_id,
                model=IMAGE_MODEL,
                prompt="",
                status="succeeded",
                has_media=True,
            )
        )
        await self.db.record_account_model_available(token_id, IMAGE_MODEL)

        await self.db.delete_token(token_id)

        self.assertIsNone(await self.db.get_token(token_id))
        self.assertEqual({}, await self.db.get_account_model_availability(token_id))
        async with self.db._connect() as db:
            for table in ("tasks", "token_stats", "projects"):
                cursor = await db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE token_id = ?",
                    (token_id,),
                )
                self.assertEqual(0, int((await cursor.fetchone())[0]), table)

    async def test_extension_session_expiry_index_exists_after_init_and_migration(self):
        async def assert_expiry_index_exists():
            async with self.db._connect() as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
                    ("idx_extension_plugin_sessions_expires_at",),
                )
                self.assertIsNotNone(await cursor.fetchone())

        await self.db.init_db()
        await assert_expiry_index_exists()
        await self.db.check_and_migrate_db({})
        await assert_expiry_index_exists()

    async def test_extension_session_expiry_index_exists_when_availability_backfill_has_no_tasks(self):
        async with self.db._connect(write=True) as db:
            await self.db._ensure_extension_plugin_sessions_table(db)
            self.assertFalse(await self.db._table_exists(db, "tasks"))
            await self.db._backfill_account_model_availability(db)
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
                ("idx_extension_plugin_sessions_expires_at",),
            )
            self.assertIsNotNone(await cursor.fetchone())

    async def test_real_existing_database_startup_backfill_is_executable_and_idempotent(self):
        await self.db.init_db()
        connection = sqlite3.connect(self.db_path)
        try:
            self._insert_tokens(connection, 31, 32)
            connection.execute("DROP TABLE account_model_availability")
            connection.executemany(
                """
                INSERT INTO tasks (task_id, token_id, model, prompt, result_urls, status, has_media, error_class)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("legacy-denial", 31, "image-model", "private-a", "private-url-a", "failed", 0, "model_access_denied"),
                    ("legacy-success", 31, "image-model", "private-b", "private-url-b", "succeeded", 1, ""),
                    ("legacy-membership", 32, "video-model", "private-c", "private-url-c", "failed", 0, "membership_tier"),
                ],
            )
            connection.commit()
        finally:
            connection.close()

        async def run_existing_database_startup():
            self.assertTrue(self.db.db_exists())
            await self.db.init_db()
            await self.db.check_and_migrate_db({})

        async def read_counts():
            async with self.db._connect() as db:
                cursor = await db.execute(
                    """
                    SELECT token_id, model, status, successful_generations, explicit_denials
                    FROM account_model_availability
                    ORDER BY token_id, model
                    """
                )
                return [tuple(row) for row in await cursor.fetchall()]

        await run_existing_database_startup()
        first_counts = await read_counts()
        self.assertEqual(
            [
                (31, "image-model", "available", 1, 1),
                (32, "video-model", "unavailable", 0, 1),
            ],
            first_counts,
        )

        await run_existing_database_startup()
        self.assertEqual(first_counts, await read_counts())

        source = inspect.getsource(Database._backfill_account_model_availability)
        normalized = " ".join(source.split())
        self.assertIn("from tasks", normalized.lower())
        for field in ("token_id", "model", "status", "has_media", "error_class"):
            self.assertIn(field, normalized)
        self.assertNotIn("prompt", source.lower())
        self.assertNotIn("result_urls", source.lower())
        self.assertNotIn("select * from tasks", normalized.lower())

    async def test_backfill_sql_does_not_return_tasks_for_pairs_with_existing_runtime_facts(self):
        class _CountingCursor:
            def __init__(self, cursor, owner):
                self._cursor = cursor
                self._owner = owner

            async def fetchall(self):
                rows = await self._cursor.fetchall()
                self._owner.task_rows = [tuple(row) for row in rows]
                return rows

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class _CountingDb:
            def __init__(self, db):
                self._db = db
                self.task_rows = []

            async def execute(self, sql, parameters=None):
                cursor = await self._db.execute(sql, parameters or ())
                normalized_sql = " ".join(str(sql).lower().split())
                if " from tasks" in normalized_sql:
                    return _CountingCursor(cursor, self)
                return cursor

        await self.db.init_db()
        connection = sqlite3.connect(self.db_path)
        try:
            self._insert_tokens(connection, 41, 42)
            existing_tasks = [
                (
                    f"existing-{index}",
                    41,
                    "existing-model",
                    f"private-{index}",
                    f"https://private.example/existing-{index}",
                    "succeeded",
                    1,
                    "",
                )
                for index in range(50)
            ]
            new_tasks = [
                ("new-denial", 42, "new-model", "private-a", "https://private.example/a", "failed", 0, "model_access_denied"),
                ("new-success-1", 42, "new-model", "private-b", "https://private.example/b", "succeeded", 1, ""),
                ("new-success-2", 42, "new-model", "private-c", "https://private.example/c", "succeeded", 1, ""),
            ]
            connection.executemany(
                """
                INSERT INTO tasks (task_id, token_id, model, prompt, result_urls, status, has_media, error_class)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                existing_tasks + new_tasks,
            )
            connection.commit()
        finally:
            connection.close()

        await self.db.record_account_model_available(41, "existing-model")
        async with self.db._connect(write=True) as raw_db:
            counting_db = _CountingDb(raw_db)
            await self.db._backfill_account_model_availability(counting_db)
            await raw_db.commit()

        self.assertEqual(1, len(counting_db.task_rows), counting_db.task_rows)
        self.assertEqual((42, "new-model"), counting_db.task_rows[0][:2])

        async with self.db._connect() as db:
            cursor = await db.execute(
                """
                SELECT token_id, model, status, successful_generations, explicit_denials
                FROM account_model_availability
                WHERE token_id IN (41, 42)
                ORDER BY token_id
                """
            )
            rows = [tuple(row) for row in await cursor.fetchall()]
        self.assertEqual(
            [
                (41, "existing-model", "available", 1, 0),
                (42, "new-model", "available", 2, 1),
            ],
            rows,
        )

    async def test_migration_backfills_only_task_metadata_without_reading_sensitive_fields(self):
        await self.db.init_db()
        connection = sqlite3.connect(self.db_path)
        try:
            self._insert_tokens(connection, 7, 8, 9)
            connection.execute("DROP TABLE account_model_availability")
            connection.executemany(
                """
                INSERT INTO tasks (task_id, token_id, model, prompt, result_urls, status, has_media, error_class)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("task-1", 7, "image-model", "private prompt", "https://private.example/media", "failed", 0, "model_access_denied"),
                    ("task-2", 7, "image-model", "another private prompt", "https://private.example/media-2", "succeeded", 1, ""),
                    ("task-3", 8, "video-model", "private video prompt", "https://private.example/video", "failed", 0, "membership_tier"),
                    ("task-4", 9, "unknown-model", "private prompt", "https://private.example/no-media", "succeeded", 0, ""),
                ],
            )
            connection.commit()
        finally:
            connection.close()

        await self.db.check_and_migrate_db({})

        self.assertEqual(
            {
                "image-model": {
                    "status": "available",
                    "error_class": "",
                    "last_verified_at": ANY,
                }
            },
            await self.db.get_account_model_availability(7),
        )
        self.assertEqual(
            {
                "video-model": {
                    "status": "unavailable",
                    "error_class": "membership_tier",
                    "last_verified_at": ANY,
                }
            },
            await self.db.get_account_model_availability(8),
        )
        self.assertEqual({}, await self.db.get_account_model_availability(9))

    async def test_availability_is_scoped_to_account_and_success_recovers_a_denial(self):
        await self.db.init_db()
        connection = sqlite3.connect(self.db_path)
        try:
            self._insert_tokens(connection, 1, 2)
            connection.commit()
        finally:
            connection.close()

        await self.db.record_account_model_unavailable(1, "model-a", "model_access_denied")
        await self.db.record_account_model_unavailable(2, "model-a", "membership_tier")
        await self.db.record_account_model_available(1, "model-a")

        self.assertEqual(
            {
                "model-a": {
                    "status": "available",
                    "error_class": "",
                    "last_verified_at": ANY,
                }
            },
            await self.db.get_account_model_availability(1),
        )
        self.assertEqual(
            {
                "model-a": {
                    "status": "unavailable",
                    "error_class": "membership_tier",
                    "last_verified_at": ANY,
                }
            },
            await self.db.get_account_model_availability(2),
        )

    async def test_only_explicit_access_denials_are_persisted(self):
        await self.db.init_db()
        connection = sqlite3.connect(self.db_path)
        try:
            self._insert_tokens(connection, 1)
            connection.commit()
        finally:
            connection.close()

        await self.db.record_account_model_unavailable(1, "model-a", "quota_exhausted")
        await self.db.record_account_model_unavailable(1, "model-a", "rate_limited")

        self.assertEqual({}, await self.db.get_account_model_availability(1))

    async def test_idempotent_diagnostic_image_membership_denial_records_unavailable_and_stops(self):
        old_cache_enabled = config.cache_enabled
        old_captcha_method = config.captcha_method
        config.set_cache_enabled(False)
        config.set_captcha_method("yescaptcha")
        try:
            await self.db.init_db()
            await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
            handler, flow, token_id = await self._build_generation_handler()
            flow.mode = "membership"

            await asyncio.wait_for(
                self._run_generation(
                    handler,
                    model=IMAGE_MODEL,
                    key="diagnostic-image-membership",
                    diagnostic_token_id=token_id,
                ),
                timeout=0.5,
            )

            availability = await self.db.get_account_model_availability(token_id)
            self.assertEqual(1, flow.image_submit_calls)
            self.assertEqual("unavailable", availability[IMAGE_MODEL]["status"])
            self.assertEqual("membership_tier", availability[IMAGE_MODEL]["error_class"])
        finally:
            config.set_cache_enabled(old_cache_enabled)
            config.set_captcha_method(old_captcha_method)

    async def test_idempotent_diagnostic_video_membership_denial_records_unavailable_and_stops(self):
        old_cache_enabled = config.cache_enabled
        old_captcha_method = config.captcha_method
        config.set_cache_enabled(False)
        config.set_captcha_method("yescaptcha")
        try:
            await self.db.init_db()
            await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
            handler, flow, token_id = await self._build_generation_handler()
            flow.mode = "membership"

            await asyncio.wait_for(
                self._run_generation(
                    handler,
                    model=VIDEO_MODEL,
                    key="diagnostic-video-membership",
                    diagnostic_token_id=token_id,
                ),
                timeout=0.5,
            )

            availability = await self.db.get_account_model_availability(token_id)
            self.assertEqual(1, flow.video_submit_calls)
            self.assertEqual("unavailable", availability[VIDEO_MODEL]["status"])
            self.assertEqual("membership_tier", availability[VIDEO_MODEL]["error_class"])
        finally:
            config.set_cache_enabled(old_cache_enabled)
            config.set_captcha_method(old_captcha_method)

    async def test_normal_membership_tier_gate_records_unavailable_without_rerouting(self):
        old_cache_enabled = config.cache_enabled
        old_captcha_method = config.captcha_method
        config.set_cache_enabled(False)
        config.set_captcha_method("yescaptcha")
        try:
            await self.db.init_db()
            await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
            handler, flow, token_id = await self._build_generation_handler()

            with patch(
                "src.services.generation_handler.supports_model_for_tier",
                return_value=False,
            ):
                await self._run_generation(
                    handler,
                    model=IMAGE_MODEL,
                    key=None,
                    diagnostic_token_id=token_id,
                )

            availability = await self.db.get_account_model_availability(token_id)
            self.assertEqual(0, flow.image_submit_calls)
            self.assertEqual("unavailable", availability[IMAGE_MODEL]["status"])
            self.assertEqual("membership_tier", availability[IMAGE_MODEL]["error_class"])
        finally:
            config.set_cache_enabled(old_cache_enabled)
            config.set_captcha_method(old_captcha_method)

    async def test_normal_and_idempotent_image_and_video_successes_record_available(self):
        old_cache_enabled = config.cache_enabled
        old_captcha_method = config.captcha_method
        config.set_cache_enabled(False)
        config.set_captcha_method("yescaptcha")
        try:
            await self.db.init_db()
            await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
            handler, flow, token_id = await self._build_generation_handler()

            cases = (
                (IMAGE_MODEL, None),
                (IMAGE_MODEL, "success-image-idempotent"),
                (VIDEO_MODEL, None),
                (VIDEO_MODEL, "success-video-idempotent"),
            )
            for model, key in cases:
                with self.subTest(model=model, key=key):
                    await self._run_generation(
                        handler,
                        model=model,
                        key=key,
                        diagnostic_token_id=token_id,
                    )
                    availability = await self.db.get_account_model_availability(token_id)
                    self.assertEqual("available", availability[model]["status"])
                    self.assertEqual("", availability[model]["error_class"])

            self.assertEqual(2, flow.image_submit_calls)
            self.assertEqual(2, flow.video_submit_calls)
        finally:
            config.set_cache_enabled(old_cache_enabled)
            config.set_captcha_method(old_captcha_method)

    async def test_recaptcha_quota_rate_limit_and_upstream_5xx_do_not_create_red_facts(self):
        old_cache_enabled = config.cache_enabled
        old_captcha_method = config.captcha_method
        config.set_cache_enabled(False)
        config.set_captcha_method("yescaptcha")
        try:
            await self.db.init_db()
            await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
            handler, flow, token_id = await self._build_generation_handler()

            for mode in ("recaptcha", "quota", "rate_limit", "upstream_5xx"):
                with self.subTest(mode=mode):
                    flow.mode = mode
                    submits_before = flow.image_submit_calls
                    await self._run_generation(
                        handler,
                        model=IMAGE_MODEL,
                        key=None,
                        diagnostic_token_id=token_id,
                    )
                    self.assertEqual(submits_before + 1, flow.image_submit_calls)
                    self.assertEqual({}, await self.db.get_account_model_availability(token_id))
                    await handler.token_manager.record_success(token_id)
        finally:
            config.set_cache_enabled(old_cache_enabled)
            config.set_captcha_method(old_captcha_method)

    async def test_normal_video_poll_model_access_denial_records_unavailable(self):
        await self._assert_video_poll_denial_recorded(
            error_class="model_access_denied",
            key=None,
        )

    async def test_idempotent_video_poll_model_access_denial_records_unavailable(self):
        await self._assert_video_poll_denial_recorded(
            error_class="model_access_denied",
            key="poll-model-access-denied",
        )

    async def test_normal_video_poll_membership_denial_records_unavailable(self):
        await self._assert_video_poll_denial_recorded(
            error_class="membership_tier",
            key=None,
        )

    async def test_idempotent_video_poll_membership_denial_records_unavailable(self):
        await self._assert_video_poll_denial_recorded(
            error_class="membership_tier",
            key="poll-membership-tier",
        )

    async def test_generation_records_media_success_and_explicit_denial_but_ignores_temporary_failure(self):
        old_cache_enabled = config.cache_enabled
        old_captcha_method = config.captcha_method
        config.set_cache_enabled(False)
        config.set_captcha_method("yescaptcha")
        try:
            await self.db.init_db()
            await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
            handler, flow, token_id = await self._build_generation_handler()
            stored_token = await self.db.get_token(token_id)
            self.assertEqual(999, stored_token.credits)
            self.assertEqual(999, await self.db.get_available_token_credits(token_id))
            self.assertTrue(stored_token.is_active)
            self.assertEqual(PAYGATE_TIER_NOT_PAID, stored_token.user_paygate_tier)
            self.assertEqual(1, len(await handler.token_manager.get_active_tokens()))
            self.assertTrue(await handler.token_manager.ensure_valid_token(stored_token))
            selected = await handler.load_balancer.select_token(
                for_image_generation=True,
                model=IMAGE_MODEL,
                reserve=True,
                enforce_concurrency_filter=True,
                track_pending=False,
                require_available_credits=True,
            )
            self.assertIsNotNone(selected)
            await handler.concurrency_manager.release_image(selected.id)

            success_chunks = await self._run_image_generation(handler, "availability-success")
            availability = await self.db.get_account_model_availability(token_id)
            self.assertEqual(
                "available",
                availability.get(IMAGE_MODEL, {}).get("status"),
                {"availability": availability, "chunks": success_chunks},
            )

            flow.mode = "denied"
            await self._run_image_generation(handler, "availability-denied")
            denied = await self.db.get_account_model_availability(token_id)
            self.assertEqual("unavailable", denied[IMAGE_MODEL]["status"])
            self.assertEqual("model_access_denied", denied[IMAGE_MODEL]["error_class"])

            flow.mode = "temporary_failure"
            await self._run_image_generation(handler, "availability-temporary")
            self.assertEqual(denied, await self.db.get_account_model_availability(token_id))

            flow.mode = "success"
            await self._run_image_generation(handler, "availability-recovery-success")
            recovered = await self.db.get_account_model_availability(token_id)
            self.assertEqual("available", recovered[IMAGE_MODEL]["status"])
            self.assertEqual("", recovered[IMAGE_MODEL]["error_class"])
        finally:
            config.set_cache_enabled(old_cache_enabled)
            config.set_captcha_method(old_captcha_method)


if __name__ == "__main__":
    unittest.main()
