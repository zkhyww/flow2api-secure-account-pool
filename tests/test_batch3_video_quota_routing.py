import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.account_tiers import PAYGATE_TIER_NOT_PAID
from src.core.config import config
from src.core.database import Database
from src.core.models import Project, Token
from src.services.concurrency_manager import ConcurrencyManager
from src.services.generation_handler import GenerationHandler
from src.services.load_balancer import LoadBalancer
from src.services.proxy_manager import ProxyManager
from src.services.token_manager import TokenManager


VIDEO_MODEL = "veo_3_1_t2v_fast_landscape"
QUOTA_RESERVATION_UNIT = 1


class _FlowBoundaryError(Exception):
    def __init__(self, *, status_code, error_code):
        super().__init__(error_code)
        self.status_code = status_code
        self.error_code = error_code


class _VideoQuotaFlowClient:
    def __init__(self):
        self.submit_calls = 0
        self.submit_token_ids = []
        self.failures_by_token = {}
        self.block_submit = False
        self.submit_started = asyncio.Event()
        self.second_submit_started = asyncio.Event()
        self.submit_release = asyncio.Event()
        self.block_poll = False
        self.poll_started = asyncio.Event()
        self.poll_release = asyncio.Event()

    def clear_request_fingerprint(self):
        return None

    async def prefill_remote_browser_pool(self, **kwargs):
        return None

    async def generate_video_text(self, **kwargs):
        token_id = kwargs.get("token_id")
        self.submit_calls += 1
        self.submit_token_ids.append(token_id)
        if self.submit_calls == 1:
            self.submit_started.set()
        if self.submit_calls >= 2:
            self.second_submit_started.set()
        if self.block_submit:
            await self.submit_release.wait()

        failure = self.failures_by_token.get(token_id)
        if failure is not None:
            raise failure

        return {
            "operations": [
                {
                    "operation": {"name": f"video-operation-{self.submit_calls}"},
                    "sceneId": "scene-ref",
                }
            ]
        }

    async def check_video_status(self, at, operations):
        self.poll_started.set()
        if self.block_poll:
            await self.poll_release.wait()

        operation_name = (operations[0].get("operation") or {}).get(
            "name", "video-operation-ref"
        )
        return {
            "operations": [
                {
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
                    "mediaName": "video-media-ref",
                    "operation": {
                        "name": operation_name,
                        "metadata": {
                            "video": {
                                "mediaName": "video-media-ref",
                                "mediaGenerationId": "video-media-ref",
                                "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
                                "model": "video-model-ref",
                                "duration": 8,
                            }
                        },
                    },
                }
            ]
        }

    async def get_media_url_redirect(self, *args, **kwargs):
        return "video-result-ref"


async def _collect(handler, key, *, stream=False):
    chunks = []
    async for chunk in handler.handle_generation(
        model=VIDEO_MODEL,
        prompt="",
        images=None,
        stream=stream,
        idempotency_key=key,
    ):
        chunks.append(chunk)
    return chunks


class Batch3VideoQuotaRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_captcha = config.captcha_method
        self.old_call_mode = config.call_logic_mode
        self.old_cache_enabled = config.cache_enabled
        config.set_captcha_method("yescaptcha")
        config.set_call_logic_mode("default")
        config.set_cache_enabled(False)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "batch3-video-quota.db"))
        await self.db.init_db()
        await self.db.init_config_from_toml(
            config.get_raw_config(), is_first_startup=True
        )
        self.flow = _VideoQuotaFlowClient()

    async def asyncTearDown(self):
        self.flow.submit_release.set()
        self.flow.poll_release.set()
        config.set_captcha_method(self.old_captcha)
        config.set_call_logic_mode(self.old_call_mode)
        config.set_cache_enabled(self.old_cache_enabled)
        self.temp_dir.cleanup()

    async def _add_token(self, *, credits, ordinal):
        token_id = await self.db.add_token(
            Token(
                st=f"fixture-session-{ordinal}",
                at=None,
                at_expires=datetime.now(timezone.utc) + timedelta(hours=2),
                email="",
                credits=credits,
                user_paygate_tier=PAYGATE_TIER_NOT_PAID,
                image_concurrency=-1,
                video_concurrency=-1,
            )
        )
        await self.db.add_project(
            Project(
                project_id=f"project-{ordinal}",
                token_id=token_id,
                project_name="fixture",
            )
        )
        return await self.db.get_token(token_id)

    async def _build_handler(self):
        tokens = await self.db.get_active_tokens()
        token_manager = TokenManager(self.db, self.flow)
        token_manager._get_project_pool_size = lambda: 1
        token_manager._should_refresh_at = lambda token: False
        for token in tokens:
            token_manager._mark_at_valid(token.id)

        concurrency_manager = ConcurrencyManager()
        await concurrency_manager.initialize(tokens)
        load_balancer = LoadBalancer(token_manager, concurrency_manager)
        handler = GenerationHandler(
            self.flow,
            token_manager,
            load_balancer,
            self.db,
            concurrency_manager,
            ProxyManager(self.db),
        )
        return handler, concurrency_manager

    def _assert_quota_state(self, task, *, state, reserved):
        self.assertEqual(getattr(task, "quota_state", None), state)
        self.assertEqual(getattr(task, "quota_reserved", None), reserved)

    async def _reserved_total(self):
        async with self.db._connect() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(quota_reserved), 0) FROM tasks"
            )
            return int((await cursor.fetchone())[0] or 0)

    async def _wait_for_task_status(self, key, statuses, timeout=1.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            task = await self.db.get_task_by_idempotency_key(key)
            if task is not None and task.status in statuses:
                return task
            await asyncio.sleep(0.01)
        self.fail(f"task did not reach one of the expected statuses: {sorted(statuses)}")

    async def _assert_authentication_account_is_persistently_excluded(self, token_id):
        token = await self.db.get_token(token_id)
        self.assertTrue(token.is_active)
        self.assertEqual("backoff", token.auth_state)
        self.assertEqual("oauth_callback_missing", token.last_auth_error_class)

    async def test_zero_credit_first_account_is_skipped_before_video_submit(self):
        empty = await self._add_token(credits=0, ordinal=1)
        funded = await self._add_token(credits=2, ordinal=2)
        handler, _ = await self._build_handler()

        await _collect(handler, "video-route-funded")

        task = await self.db.get_task_by_idempotency_key("video-route-funded")
        self.assertEqual(self.flow.submit_token_ids, [funded.id])
        self.assertNotIn(empty.id, self.flow.submit_token_ids)
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.token_id, funded.id)
        self._assert_quota_state(task, state="settled", reserved=0)

    async def test_all_zero_credit_accounts_never_submit_video(self):
        await self._add_token(credits=0, ordinal=1)
        await self._add_token(credits=0, ordinal=2)
        handler, _ = await self._build_handler()

        chunks = await _collect(handler, "video-route-no-credit")

        self.assertEqual(self.flow.submit_token_ids, [])
        self.assertTrue(chunks)

    async def test_same_key_replay_reserves_submits_and_settles_video_once(self):
        token = await self._add_token(credits=2, ordinal=1)
        handler_a, _ = await self._build_handler()
        handler_b, _ = await self._build_handler()
        key = "video-quota-replay"

        await _collect(handler_a, key)
        await _collect(handler_b, key)
        await _collect(handler_a, key)

        task = await self.db.get_task_by_idempotency_key(key)
        refreshed_token = await self.db.get_token(token.id)
        self.assertEqual(self.flow.submit_token_ids, [token.id])
        self.assertEqual(task.status, "succeeded")
        self._assert_quota_state(task, state="settled", reserved=0)
        self.assertEqual(refreshed_token.credits, 1)

    async def test_two_handlers_different_keys_cannot_oversell_last_video_unit(self):
        await self._add_token(credits=QUOTA_RESERVATION_UNIT, ordinal=1)
        self.flow.block_submit = True
        handler_a, _ = await self._build_handler()
        handler_b, _ = await self._build_handler()

        first = asyncio.create_task(_collect(handler_a, "video-last-unit-a"))
        await asyncio.wait_for(self.flow.submit_started.wait(), timeout=0.5)
        second = asyncio.create_task(_collect(handler_b, "video-last-unit-b"))
        second_submit_waiter = asyncio.create_task(
            self.flow.second_submit_started.wait()
        )
        try:
            done, _ = await asyncio.wait(
                {second, second_submit_waiter},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=0.25,
            )
            self.assertNotIn(
                second_submit_waiter,
                done,
                "the final video credit was submitted twice",
            )
            self.assertEqual(self.flow.submit_calls, 1)
            self.assertEqual(await self._reserved_total(), 1)
        finally:
            self.flow.submit_release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            if not second_submit_waiter.done():
                second_submit_waiter.cancel()
            await asyncio.gather(second_submit_waiter, return_exceptions=True)

        self.assertEqual(self.flow.submit_calls, 1)

    async def test_video_submit_timeout_keeps_one_frozen_reservation(self):
        token = await self._add_token(credits=1, ordinal=1)
        self.flow.failures_by_token[token.id] = asyncio.TimeoutError()
        handler, _ = await self._build_handler()
        key = "video-submit-timeout"

        await _collect(handler, key)

        task = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_token_ids, [token.id])
        self.assertEqual(task.status, "unknown")
        self._assert_quota_state(task, state="reserved", reserved=1)
        self.assertEqual(await self.db.get_available_token_credits(token.id), 0)

    async def test_cancelled_video_submit_keeps_one_frozen_reservation(self):
        token = await self._add_token(credits=1, ordinal=1)
        self.flow.block_submit = True
        handler, _ = await self._build_handler()
        key = "video-submit-cancelled"

        consumer = asyncio.create_task(_collect(handler, key))
        await asyncio.wait_for(self.flow.submit_started.wait(), timeout=0.5)
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer

        task = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_token_ids, [token.id])
        self.assertEqual(task.status, "unknown")
        self._assert_quota_state(task, state="reserved", reserved=1)
        self.assertEqual(await self.db.get_available_token_credits(token.id), 0)

    async def test_video_429_cools_only_hit_account_then_routes_once_to_second(self):
        first = await self._add_token(credits=2, ordinal=1)
        second = await self._add_token(credits=2, ordinal=2)
        handler, concurrency_manager = await self._build_handler()
        second_remaining_before = await concurrency_manager.get_video_remaining(second.id)
        self.flow.failures_by_token[first.id] = _FlowBoundaryError(
            status_code=429,
            error_code="RATE_LIMITED",
        )
        key = "video-route-after-429"

        await _collect(handler, key)

        task = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_token_ids, [first.id, second.id])
        self.assertFalse(await concurrency_manager.can_use_video(first.id))
        self.assertEqual(
            await concurrency_manager.get_video_remaining(second.id),
            second_remaining_before,
        )
        self.assertTrue((await self.db.get_token(first.id)).is_active)
        self.assertTrue((await self.db.get_token(second.id)).is_active)
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.token_id, second.id)
        self._assert_quota_state(task, state="settled", reserved=0)

    async def test_video_authentication_failure_persistently_excludes_then_switches_once(self):
        first = await self._add_token(credits=2, ordinal=1)
        second = await self._add_token(credits=2, ordinal=2)
        third = await self._add_token(credits=2, ordinal=3)
        handler, _ = await self._build_handler()
        self.flow.failures_by_token[first.id] = _FlowBoundaryError(
            status_code=401,
            error_code="AUTHENTICATION",
        )
        key = "video-route-authentication"

        await _collect(handler, key)

        task = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_token_ids, [first.id, second.id])
        self.assertNotIn(third.id, self.flow.submit_token_ids)
        await self._assert_authentication_account_is_persistently_excluded(first.id)
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(task.token_id, second.id)
        self._assert_quota_state(task, state="settled", reserved=0)

    async def test_video_content_policy_failure_never_switches_accounts(self):
        first = await self._add_token(credits=2, ordinal=1)
        await self._add_token(credits=2, ordinal=2)
        handler, _ = await self._build_handler()
        self.flow.failures_by_token[first.id] = _FlowBoundaryError(
            status_code=400,
            error_code="CONTENT_POLICY",
        )
        key = "video-route-content-policy"

        await _collect(handler, key)

        task = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_token_ids, [first.id])
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.error_class, "content_policy")
        self._assert_quota_state(task, state="released", reserved=0)

    async def test_video_model_access_denied_is_public_request_failure_without_account_penalty(self):
        first = await self._add_token(credits=2, ordinal=1)
        second = await self._add_token(credits=2, ordinal=2)
        handler, _ = await self._build_handler()
        self.flow.failures_by_token[first.id] = _FlowBoundaryError(
            status_code=403,
            error_code="PUBLIC_ERROR_MODEL_ACCESS_DENIED",
        )
        key = "video-model-access-denied"

        chunks = await _collect(handler, key)

        task = await self.db.get_task_by_idempotency_key(key)
        first_after = await self.db.get_token(first.id)
        second_after = await self.db.get_token(second.id)
        first_stats = await self.db.get_token_stats(first.id)
        payload = json.loads(chunks[-1])
        async with self.db._connect() as conn:
            cursor = await conn.execute(
                "SELECT response_body, status_code FROM request_logs ORDER BY id DESC LIMIT 1"
            )
            request_log = await cursor.fetchone()

        self.assertEqual(self.flow.submit_token_ids, [first.id])
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.error_class, "model_access_denied")
        self._assert_quota_state(task, state="released", reserved=0)
        self.assertTrue(first_after.is_active)
        self.assertFalse(first_after.ban_reason)
        self.assertEqual(first_stats.consecutive_error_count if first_stats else 0, 0)
        self.assertTrue(second_after.is_active)
        self.assertEqual("model_access_denied", payload["error"]["code"])
        self.assertEqual(403, payload["error"]["status_code"])
        self.assertIsNotNone(request_log)
        logged_payload = json.loads(request_log[0])
        self.assertEqual(
            {"status": "failed", "error_class": "model_access_denied", "has_media": False},
            logged_payload,
        )
        self.assertEqual(403, request_log[1])

    async def test_idempotent_video_terminal_failure_is_logged_before_stream_consumer_closes(self):
        await self._add_token(credits=2, ordinal=1)
        handler, _ = await self._build_handler()
        sentinel = "UPSTREAM_PRIVATE_IDEMPOTENT_FAILURE_SENTINEL"

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
            idempotency_key="video-request-log-terminal",
        )
        try:
            while True:
                chunk = await anext(consumer)
                if "错误:" in chunk:
                    break
        finally:
            await consumer.aclose()

        async with self.db._connect() as conn:
            cursor = await conn.execute(
                "SELECT status_code, status_text, response_body "
                "FROM request_logs ORDER BY id DESC LIMIT 1"
            )
            request_log = await cursor.fetchone()

        self.assertIsNotNone(request_log)
        self.assertGreaterEqual(int(request_log[0]), 400)
        self.assertEqual("failed", request_log[1])
        payload = json.loads(request_log[2])
        self.assertEqual("upstream_error", payload["error_class"])
        self.assertFalse(payload["has_media"])
        self.assertNotIn(sentinel, request_log[2])

    async def test_detached_video_polling_survives_disconnect_and_settles_reservation(self):
        token = await self._add_token(credits=1, ordinal=1)
        self.flow.block_poll = True
        handler, _ = await self._build_handler()
        key = "video-detached-polling"

        consumer = asyncio.create_task(_collect(handler, key, stream=True))
        try:
            await asyncio.wait_for(self.flow.poll_started.wait(), timeout=0.5)
            polling_task = await self._wait_for_task_status(key, {"polling"})
            self.assertEqual(polling_task.token_id, token.id)

            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer

            self.flow.poll_release.set()
            completed = await self._wait_for_task_status(key, {"succeeded"})
        finally:
            self.flow.poll_release.set()
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

        self.assertEqual(self.flow.submit_token_ids, [token.id])
        self.assertEqual(completed.token_id, token.id)
        self._assert_quota_state(completed, state="settled", reserved=0)
        self.assertEqual((await self.db.get_token(token.id)).credits, 0)


if __name__ == "__main__":
    unittest.main()
