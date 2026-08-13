import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src import main
from src.core.account_tiers import PAYGATE_TIER_NOT_PAID
from src.core.database import Database
from src.core.models import Task, Token
from src.services.generation_handler import GenerationHandler


IMAGE_MODEL = "gemini-3.1-flash-image-landscape"
VIDEO_MODEL = "veo_3_1_t2v_fast_landscape"


class _TokenManager:
    def __init__(self, token):
        self.token = token
        self.record_error = AsyncMock()
        self.record_usage = AsyncMock()
        self.record_success = AsyncMock()

    async def ensure_valid_token(self, token):
        return token

    async def ensure_project_exists(self, token_id):
        return "project-ref"


class _LoadBalancer:
    def __init__(self, token):
        self.token = token

    async def select_token(self, **kwargs):
        return self.token

    async def release_pending(self, token_id, **kwargs):
        return None

    async def get_unavailable_reason(self, **kwargs):
        return None


class _ProxyManager:
    pass


class _ExplodingFlowClient:
    def __init__(self, sentinel):
        self.sentinel = sentinel

    def clear_request_fingerprint(self):
        return None

    async def prefill_remote_browser_pool(self, **kwargs):
        return None

    async def generate_image(self, **kwargs):
        raise RuntimeError(self.sentinel)


class Batch2LifespanRecoveryStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_starts_recovery_without_asyncio_local_shadowing(self):
        recovery_started = asyncio.Event()

        async def recover():
            recovery_started.set()

        fake_db = SimpleNamespace(
            db_exists=Mock(return_value=True),
            init_db=AsyncMock(),
            check_and_migrate_db=AsyncMock(),
            reload_config_to_memory=AsyncMock(),
            get_captcha_config=AsyncMock(return_value=SimpleNamespace(captcha_method="yescaptcha")),
        )
        fake_file_cache = SimpleNamespace(
            set_timeout=Mock(),
            refresh_cleanup_task=AsyncMock(return_value=False),
            stop_cleanup_task=AsyncMock(),
        )
        fake_handler = SimpleNamespace(
            file_cache=fake_file_cache,
            recover_incomplete_tasks=recover,
        )
        fake_token_manager = SimpleNamespace(
            get_all_tokens=AsyncMock(return_value=[]),
            start_protocol_refresher=Mock(),
            stop_protocol_refresher=AsyncMock(),
            auto_unban_429_tokens=AsyncMock(),
        )
        fake_concurrency = SimpleNamespace(initialize=AsyncMock())
        fake_config = SimpleNamespace(
            get_raw_config=Mock(return_value={}),
            cache_timeout=0,
            cache_enabled=False,
            captcha_method="yescaptcha",
            server_host="127.0.0.1",
            server_port=8000,
        )

        with patch.object(main, "db", fake_db), \
             patch.object(main, "generation_handler", fake_handler), \
             patch.object(main, "token_manager", fake_token_manager), \
             patch.object(main, "concurrency_manager", fake_concurrency), \
             patch.object(main, "config", fake_config):
            async with main.lifespan(None):
                await asyncio.sleep(0)
                self.assertTrue(recovery_started.is_set())


class Batch2RecoveryConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "recovery.db"))
        await self.db.init_db()
        token_id = await self.db.add_token(
            Token(
                st="",
                at=None,
                email="",
                user_paygate_tier=PAYGATE_TIER_NOT_PAID,
                image_concurrency=-1,
                video_concurrency=-1,
            )
        )
        self.token = await self.db.get_token(token_id)
        await self.db.create_task(
            Task(
                task_id="recover-a",
                token_id=token_id,
                model="veo_3_1_t2v_fast",
                prompt="",
                status="accepted",
            )
        )
        await self.db.create_task(
            Task(
                task_id="recover-b",
                token_id=token_id,
                model="veo_3_1_t2v_fast",
                prompt="",
                status="polling",
            )
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_recovery_starts_all_accepted_and_polling_tasks_before_any_finishes(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.db = self.db
        handler.token_manager = _TokenManager(self.token)
        handler.flow_client = object()
        handler._background_poll_tasks = set()

        started = []
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_poll(**kwargs):
            started.append(kwargs["task_id"])
            if len(started) >= 2:
                both_started.set()
            await release.wait()
            return []

        handler._collect_detached_video_poll = AsyncMock(side_effect=fake_poll)
        recovery = asyncio.create_task(handler.recover_incomplete_tasks())
        try:
            await asyncio.wait_for(both_started.wait(), timeout=0.5)
            self.assertCountEqual(started, ["recover-a", "recover-b"])
        finally:
            release.set()
            await recovery


class Batch2VideoRequestLogFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def _build_handler(self):
        temp_dir = tempfile.TemporaryDirectory()
        db = Database(str(Path(temp_dir.name) / "video-request-log.db"))
        await db.init_db()
        token_id = await db.add_token(
            Token(
                st="",
                at=None,
                email="",
                user_paygate_tier=PAYGATE_TIER_NOT_PAID,
                image_concurrency=-1,
                video_concurrency=-1,
            )
        )
        token = await db.get_token(token_id)
        handler = GenerationHandler(
            _ExplodingFlowClient("unused"),
            _TokenManager(token),
            _LoadBalancer(token),
            db,
            object(),
            _ProxyManager(),
        )
        return temp_dir, db, handler, token

    async def _latest_request_log(self, db):
        async with db._connect() as conn:
            conn.row_factory = None
            cursor = await conn.execute(
                "SELECT status_code, status_text, progress, response_body "
                "FROM request_logs ORDER BY id DESC LIMIT 1"
            )
            return await cursor.fetchone()

    async def test_video_terminal_failure_is_logged_before_stream_consumer_closes(self):
        sentinel = "UPSTREAM_PRIVATE_FAILURE_SENTINEL"
        temp_dir, db, handler, _token = await self._build_handler()
        try:
            async def failing_video(*_args, **kwargs):
                generation_result = kwargs["generation_result"]
                handler._mark_generation_failed(generation_result, sentinel)
                yield handler._create_stream_chunk(f"错误: {sentinel}\n")
                yield handler._create_error_response(sentinel, status_code=502)

            handler._handle_video_generation = failing_video
            consumer = handler.handle_generation(
                model=VIDEO_MODEL,
                prompt="fixture",
                stream=True,
            )
            terminal_chunk = ""
            try:
                while True:
                    chunk = await anext(consumer)
                    if "错误:" in chunk:
                        terminal_chunk = chunk
                        break
            finally:
                await consumer.aclose()

            self.assertIn(sentinel, terminal_chunk)
            request_log = await self._latest_request_log(db)
            self.assertIsNotNone(request_log)
            status_code, status_text, _progress, response_body = request_log
            self.assertGreaterEqual(int(status_code), 400)
            self.assertEqual("failed", status_text)
            payload = json.loads(response_body)
            self.assertEqual("failed", payload["status"])
            self.assertEqual("upstream_error", payload["error_class"])
            self.assertFalse(payload["has_media"])
            self.assertNotIn(sentinel, response_body)
        finally:
            temp_dir.cleanup()

    async def test_video_exception_finalizes_request_log_with_safe_failure(self):
        sentinel = "UPSTREAM_PRIVATE_EXCEPTION_SENTINEL"
        temp_dir, db, handler, _token = await self._build_handler()
        try:
            async def exploding_video(*_args, **_kwargs):
                if False:
                    yield ""
                raise RuntimeError(sentinel)

            handler._handle_video_generation = exploding_video
            chunks = []
            async for chunk in handler.handle_generation(
                model=VIDEO_MODEL,
                prompt="fixture",
                stream=True,
            ):
                chunks.append(chunk)

            request_log = await self._latest_request_log(db)
            self.assertIsNotNone(request_log)
            status_code, status_text, _progress, response_body = request_log
            self.assertGreaterEqual(int(status_code), 400)
            self.assertEqual("failed", status_text)
            payload = json.loads(response_body)
            self.assertEqual("failed", payload["status"])
            self.assertEqual("upstream_error", payload["error_class"])
            self.assertNotIn(sentinel, response_body)
            self.assertNotIn(sentinel, "".join(chunks))
        finally:
            temp_dir.cleanup()


class Batch2SafeExceptionResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_idempotent_submit_exception_never_exposes_raw_upstream_text(self):
        sentinel = "UPSTREAM_BODY_SENTINEL_DO_NOT_EXPOSE"
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "safe-errors.db"))
            await db.init_db()
            token_id = await db.add_token(
                Token(
                    st="",
                    at=None,
                    email="",
                    user_paygate_tier=PAYGATE_TIER_NOT_PAID,
                    image_concurrency=-1,
                    video_concurrency=-1,
                )
            )
            token = await db.get_token(token_id)
            handler = GenerationHandler(
                _ExplodingFlowClient(sentinel),
                _TokenManager(token),
                _LoadBalancer(token),
                db,
                object(),
                _ProxyManager(),
            )

            chunks = []
            async for chunk in handler.handle_generation(
                model=IMAGE_MODEL,
                prompt="",
                images=None,
                stream=False,
                idempotency_key="safe-error-key",
            ):
                chunks.append(chunk)

            self.assertNotIn(sentinel, "".join(chunks))

            task = await db.get_task_by_idempotency_key("safe-error-key")
            self.assertIsNotNone(task)
            task_snapshot = "|".join(
                str(value or "")
                for value in (
                    task.task_id,
                    task.model,
                    task.prompt,
                    task.status,
                    task.error_message,
                    task.error_class,
                )
            )
            self.assertNotIn(sentinel, task_snapshot)
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.error_class, "upstream_error")
            self.assertFalse(task.has_media)

            async with db._connect() as conn:
                cursor = await conn.execute(
                    "SELECT operation, request_body, response_body, status_text "
                    "FROM request_logs ORDER BY id"
                )
                rows = await cursor.fetchall()
            log_snapshot = "|".join(
                str(value or "")
                for row in rows
                for value in row
            )
            self.assertNotIn(sentinel, log_snapshot)


if __name__ == "__main__":
    unittest.main()
