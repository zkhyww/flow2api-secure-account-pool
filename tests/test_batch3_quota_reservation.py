import asyncio
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


IMAGE_MODEL = "gemini-3.1-flash-image-landscape"
QUOTA_RESERVATION_UNIT = 1


class _FlowBoundaryError(Exception):
    def __init__(self, *, status_code=500, error_code="UPSTREAM_FAILURE"):
        super().__init__(error_code)
        self.status_code = status_code
        self.error_code = error_code


class _QuotaFlowClient:
    def __init__(self):
        self.submit_calls = 0
        self.submit_token_ids = []
        self.mode = "success"
        self.block_submit = False
        self.submit_started = asyncio.Event()
        self.second_submit_started = asyncio.Event()
        self.submit_release = asyncio.Event()

    def clear_request_fingerprint(self):
        return None

    async def prefill_remote_browser_pool(self, **kwargs):
        return None

    async def generate_image(self, **kwargs):
        self.submit_calls += 1
        self.submit_token_ids.append(kwargs.get("token_id"))
        if self.submit_calls == 1:
            self.submit_started.set()
        if self.submit_calls >= 2:
            self.second_submit_started.set()
        if self.block_submit:
            await self.submit_release.wait()
        if self.mode == "timeout":
            raise asyncio.TimeoutError()
        if self.mode == "failure":
            raise _FlowBoundaryError()
        return (
            {
                "media": [
                    {
                        "name": "media-ref",
                        "image": {"generatedImage": {"fifeUrl": "media-ref"}},
                    }
                ]
            },
            "session-ref",
            {},
        )


async def _collect(handler, key):
    chunks = []
    async for chunk in handler.handle_generation(
        model=IMAGE_MODEL,
        prompt="",
        images=None,
        stream=False,
        idempotency_key=key,
    ):
        chunks.append(chunk)
    return chunks


class Batch3QuotaReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_captcha = config.captcha_method
        self.old_call_mode = config.call_logic_mode
        self.old_cache_enabled = config.cache_enabled
        config.set_captcha_method("yescaptcha")
        config.set_call_logic_mode("default")
        config.set_cache_enabled(False)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "batch3-quota.db"))
        await self.db.init_db()
        await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
        self.flow = _QuotaFlowClient()
        self.token = await self._add_token(credits=3, ordinal=1)

    async def asyncTearDown(self):
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
        token_manager = TokenManager(self.db, self.flow)
        token_manager._get_project_pool_size = lambda: 1
        token_manager._should_refresh_at = lambda token: False
        for token in await self.db.get_active_tokens():
            token_manager._mark_at_valid(token.id)

        concurrency_manager = ConcurrencyManager()
        await concurrency_manager.initialize(await self.db.get_active_tokens())
        load_balancer = LoadBalancer(token_manager, concurrency_manager)
        return GenerationHandler(
            self.flow,
            token_manager,
            load_balancer,
            self.db,
            concurrency_manager,
            ProxyManager(self.db),
        )

    def _assert_quota_state(self, task, *, state, reserved):
        self.assertEqual(getattr(task, "quota_state", None), state)
        self.assertEqual(getattr(task, "quota_reserved", None), reserved)

    async def _reserved_total(self):
        async with self.db._connect() as conn:
            cursor = await conn.execute("SELECT COALESCE(SUM(quota_reserved), 0) FROM tasks")
            return int((await cursor.fetchone())[0] or 0)

    async def _reserved_by_token(self):
        async with self.db._connect() as conn:
            cursor = await conn.execute(
                "SELECT token_id, COALESCE(SUM(quota_reserved), 0) FROM tasks GROUP BY token_id ORDER BY token_id"
            )
            return {int(row[0]): int(row[1] or 0) for row in await cursor.fetchall()}

    async def test_same_key_cross_handler_reserves_one_unit_once_and_settles_once(self):
        key = "quota-cross-handler"
        self.flow.block_submit = True
        handler_a = await self._build_handler()
        handler_b = await self._build_handler()
        first = asyncio.create_task(_collect(handler_a, key))
        await asyncio.wait_for(self.flow.submit_started.wait(), timeout=0.5)
        during_submit = await self.db.get_task_by_idempotency_key(key)
        second = asyncio.create_task(_collect(handler_b, key))
        self.flow.submit_release.set()
        await asyncio.gather(first, second)
        completed = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_calls, 1)
        self._assert_quota_state(during_submit, state="reserved", reserved=1)
        self._assert_quota_state(completed, state="settled", reserved=0)

    async def test_different_keys_one_credit_unit_cross_handler_contention_never_oversells_single_account(self):
        # Minimum reservation unit is 1 request-credit for Batch 3; upstream refreshed balance remains authoritative.
        await self.db.update_token(self.token.id, credits=QUOTA_RESERVATION_UNIT)
        self.flow.block_submit = True
        handler_a = await self._build_handler()
        handler_b = await self._build_handler()
        first = asyncio.create_task(_collect(handler_a, "quota-key-a"))
        await asyncio.wait_for(self.flow.submit_started.wait(), timeout=0.5)
        second = asyncio.create_task(_collect(handler_b, "quota-key-b"))
        second_submit_waiter = asyncio.create_task(self.flow.second_submit_started.wait())
        try:
            done, _ = await asyncio.wait(
                {second, second_submit_waiter},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=0.25,
            )
            self.assertNotIn(second_submit_waiter, done)
            self.assertEqual(self.flow.submit_calls, 1)
            self.assertEqual(await self._reserved_total(), 1)
        finally:
            self.flow.submit_release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            if not second_submit_waiter.done():
                second_submit_waiter.cancel()
            await asyncio.gather(second_submit_waiter, return_exceptions=True)
        self.assertEqual(self.flow.submit_calls, 1)
        self.assertLessEqual(await self._reserved_total(), 1)

    async def test_different_keys_one_credit_unit_each_route_to_two_accounts_without_global_serialization(self):
        await self.db.update_token(self.token.id, credits=QUOTA_RESERVATION_UNIT)
        second_token = await self._add_token(credits=QUOTA_RESERVATION_UNIT, ordinal=2)
        self.flow.block_submit = True
        handler_a = await self._build_handler()
        handler_b = await self._build_handler()
        first = asyncio.create_task(_collect(handler_a, "quota-parallel-a"))
        await asyncio.wait_for(self.flow.submit_started.wait(), timeout=0.5)
        second = asyncio.create_task(_collect(handler_b, "quota-parallel-b"))
        try:
            await asyncio.wait_for(self.flow.second_submit_started.wait(), timeout=0.5)
            self.assertEqual(self.flow.submit_token_ids, [self.token.id, second_token.id])
            reserved = await self._reserved_by_token()
            self.assertEqual(reserved.get(self.token.id, 0), 1)
            self.assertEqual(reserved.get(second_token.id, 0), 1)
        finally:
            self.flow.submit_release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(self.flow.submit_calls, 2)

    async def test_success_replay_settles_once_and_never_reserves_again(self):
        key = "quota-success-replay"
        handler = await self._build_handler()
        await _collect(handler, key)
        await _collect(handler, key)
        await _collect(handler, key)
        task = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_calls, 1)
        self.assertEqual(task.status, "succeeded")
        self._assert_quota_state(task, state="settled", reserved=0)

    async def test_explicit_submit_failure_releases_once_and_repeated_replay_cannot_underflow(self):
        key = "quota-explicit-failure"
        self.flow.mode = "failure"
        handler = await self._build_handler()
        await _collect(handler, key)
        await _collect(handler, key)
        await _collect(handler, key)
        task = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_calls, 1)
        self.assertEqual(task.status, "failed")
        self._assert_quota_state(task, state="released", reserved=0)

    async def test_submit_timeout_keeps_quota_frozen_and_retry_does_not_reserve_or_submit_again(self):
        key = "quota-timeout-unknown"
        self.flow.mode = "timeout"
        handler = await self._build_handler()
        await _collect(handler, key)
        self.flow.mode = "success"
        await _collect(handler, key)
        task = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_calls, 1)
        self.assertEqual(task.status, "unknown")
        self._assert_quota_state(task, state="reserved", reserved=1)

    async def test_cancelled_submit_keeps_one_frozen_reservation_without_leak(self):
        key = "quota-cancelled-submit"
        self.flow.block_submit = True
        handler = await self._build_handler()
        consumer = asyncio.create_task(_collect(handler, key))
        await asyncio.wait_for(self.flow.submit_started.wait(), timeout=0.5)
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        task = await self.db.get_task_by_idempotency_key(key)
        self.assertEqual(self.flow.submit_calls, 1)
        self.assertEqual(task.status, "unknown")
        self._assert_quota_state(task, state="reserved", reserved=1)


if __name__ == "__main__":
    unittest.main()
