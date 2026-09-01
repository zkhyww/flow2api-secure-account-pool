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


class _FlowBoundaryError(Exception):
    def __init__(self, *, status_code=500, error_code="UPSTREAM_FAILURE"):
        super().__init__(error_code)
        self.status_code = status_code
        self.error_code = error_code


class _ConcurrencyAwareFlowClient:
    def __init__(self):
        self.concurrency = None
        self.submit_token_ids = []
        self.submit_snapshots = []
        self.failures_by_token = {}
        self.block = False
        self.submit_started = asyncio.Event()
        self.second_submit_started = asyncio.Event()
        self.release = asyncio.Event()

    def clear_request_fingerprint(self):
        return None

    async def prefill_remote_browser_pool(self, **kwargs):
        return None

    async def generate_image(self, **kwargs):
        token_id = int(kwargs["token_id"])
        self.submit_token_ids.append(token_id)
        self.submit_snapshots.append(
            {
                candidate: await self.concurrency.get_image_inflight(candidate)
                for candidate in self.known_token_ids
            }
        )
        if len(self.submit_token_ids) == 1:
            self.submit_started.set()
        if len(self.submit_token_ids) >= 2:
            self.second_submit_started.set()
        failure = self.failures_by_token.get(token_id)
        if failure is not None:
            raise failure
        if self.block:
            await self.release.wait()
        return (
            {
                "media": [
                    {
                        "name": "media-fixture",
                        "image": {"generatedImage": {"fifeUrl": "media-fixture"}},
                    }
                ]
            },
            "generation-context",
            {},
        )


async def _collect(handler, *, key=None, diagnostic_token_id=None):
    chunks = []
    async for chunk in handler.handle_generation(
        model=IMAGE_MODEL,
        prompt="",
        images=None,
        stream=False,
        idempotency_key=key,
        diagnostic_token_id=diagnostic_token_id,
    ):
        chunks.append(chunk)
    return chunks


class ThreeAccountRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_captcha = config.captcha_method
        self.old_call_mode = config.call_logic_mode
        self.old_cache_enabled = config.cache_enabled
        config.set_captcha_method("yescaptcha")
        config.set_call_logic_mode("default")
        config.set_cache_enabled(False)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "three-account-routing.db"))
        await self.db.init_db()
        await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
        self.first = await self._add_account(ordinal=1, active=True)
        self.second = await self._add_account(ordinal=2, active=True)
        self.inactive = await self._add_account(ordinal=3, active=False)

        self.flow = _ConcurrencyAwareFlowClient()
        active_tokens = await self.db.get_active_tokens()
        self.concurrency = ConcurrencyManager()
        await self.concurrency.initialize(active_tokens)
        self.flow.concurrency = self.concurrency
        self.flow.known_token_ids = [self.first.id, self.second.id, self.inactive.id]

        self.token_manager = TokenManager(self.db, self.flow)
        self.token_manager._get_project_pool_size = lambda: 1
        self.token_manager._should_refresh_at = lambda token: False
        for token in active_tokens:
            self.token_manager._mark_at_valid(token.id)
        self.load_balancer = LoadBalancer(self.token_manager, self.concurrency)
        self.handler = GenerationHandler(
            self.flow,
            self.token_manager,
            self.load_balancer,
            self.db,
            self.concurrency,
            ProxyManager(self.db),
        )

    async def asyncTearDown(self):
        self.flow.release.set()
        config.set_captcha_method(self.old_captcha)
        config.set_call_logic_mode(self.old_call_mode)
        config.set_cache_enabled(self.old_cache_enabled)
        self.temp_dir.cleanup()

    async def _add_account(self, *, ordinal, active):
        token_id = await self.db.add_token(
            Token(
                st=f"fixture-session-{ordinal}",
                at=None,
                at_expires=datetime.now(timezone.utc) + timedelta(hours=2),
                email="",
                credits=10,
                is_active=active,
                user_paygate_tier=PAYGATE_TIER_NOT_PAID,
                image_concurrency=1,
                video_concurrency=1,
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

    async def _assert_all_released(self):
        for token_id in self.flow.known_token_ids:
            self.assertEqual(0, await self.concurrency.get_image_inflight(token_id))
            self.assertEqual(
                0,
                await self.load_balancer._get_pending_count(token_id, True, False),
            )

    async def test_real_entry_reserves_one_slot_and_saturated_account_expands_to_next_active(self):
        self.flow.block = True
        first_request = asyncio.create_task(_collect(self.handler))
        await asyncio.wait_for(self.flow.submit_started.wait(), timeout=1)
        second_request = None
        try:
            self.assertEqual([self.first.id], self.flow.submit_token_ids)
            self.assertEqual(1, await self.concurrency.get_image_inflight(self.first.id))
            self.assertEqual(0, await self.load_balancer._get_pending_count(self.first.id, True, False))

            second_request = asyncio.create_task(_collect(self.handler))
            await asyncio.wait_for(self.flow.second_submit_started.wait(), timeout=1)
            self.assertEqual([self.first.id, self.second.id], self.flow.submit_token_ids)
            self.assertNotIn(self.inactive.id, self.flow.submit_token_ids)
            self.assertEqual(1, await self.concurrency.get_image_inflight(self.first.id))
            self.assertEqual(1, await self.concurrency.get_image_inflight(self.second.id))
        finally:
            self.flow.release.set()
            tasks = [first_request]
            if second_request is not None:
                tasks.append(second_request)
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._assert_all_released()

    async def test_polling_round_robin_skips_inactive_and_diagnostic_selection_is_pinned(self):
        config.set_call_logic_mode("polling")
        await _collect(self.handler)
        await _collect(self.handler)
        await _collect(self.handler, diagnostic_token_id=self.second.id)
        self.assertEqual(
            [self.first.id, self.second.id, self.second.id],
            self.flow.submit_token_ids,
        )
        self.assertNotIn(self.inactive.id, self.flow.submit_token_ids)
        await self._assert_all_released()

    async def test_exception_and_cancellation_release_real_slots(self):
        self.flow.failures_by_token[self.first.id] = _FlowBoundaryError()
        await _collect(self.handler)
        self.assertEqual([self.first.id], self.flow.submit_token_ids)
        self.assertEqual(1, self.flow.submit_snapshots[0][self.first.id])
        await self._assert_all_released()

        self.flow.failures_by_token.clear()
        self.flow.submit_token_ids.clear()
        self.flow.submit_snapshots.clear()
        self.flow.submit_started.clear()
        self.flow.block = True
        consumer = asyncio.create_task(_collect(self.handler))
        await asyncio.wait_for(self.flow.submit_started.wait(), timeout=1)
        self.assertEqual(1, await self.concurrency.get_image_inflight(self.first.id))
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        await self._assert_all_released()

    async def test_idempotent_failover_releases_first_before_reserving_second(self):
        self.flow.failures_by_token[self.first.id] = _FlowBoundaryError(
            status_code=429,
            error_code="RATE_LIMITED",
        )
        await _collect(self.handler, key="three-account-failover")
        self.assertEqual([self.first.id, self.second.id], self.flow.submit_token_ids)
        self.assertEqual(1, self.flow.submit_snapshots[0][self.first.id])
        self.assertEqual(0, self.flow.submit_snapshots[1][self.first.id])
        self.assertEqual(1, self.flow.submit_snapshots[1][self.second.id])
        await self._assert_all_released()

    async def test_automatic_request_marks_runtime_auth_failure_and_fails_over(self):
        self.flow.failures_by_token[self.first.id] = _FlowBoundaryError(
            status_code=401,
            error_code="UNAUTHENTICATED",
        )

        chunks = await _collect(self.handler)

        self.assertEqual([self.first.id, self.second.id], self.flow.submit_token_ids)
        self.assertTrue(chunks)
        first_after = await self.db.get_token(self.first.id)
        self.assertTrue(first_after.is_active)
        self.assertEqual("backoff", first_after.auth_state)
        self.assertEqual("oauth_callback_missing", first_after.last_auth_error_class)
        await self._assert_all_released()

    async def test_idempotent_diagnostic_request_stays_pinned_to_requested_active_account(self):
        await _collect(
            self.handler,
            key="diagnostic-idempotency-pinned",
            diagnostic_token_id=self.second.id,
        )
        self.assertEqual([self.second.id], self.flow.submit_token_ids)
        self.assertNotIn(self.first.id, self.flow.submit_token_ids)
        self.assertNotIn(self.inactive.id, self.flow.submit_token_ids)
        self.assertEqual(1, self.flow.submit_snapshots[0][self.second.id])
        await self._assert_all_released()

    async def test_diagnostic_authentication_failure_stays_pinned_but_updates_auth_state(self):
        self.flow.failures_by_token[self.second.id] = _FlowBoundaryError(
            status_code=401,
            error_code="UNAUTHENTICATED",
        )

        await _collect(self.handler, diagnostic_token_id=self.second.id)

        self.assertEqual([self.second.id], self.flow.submit_token_ids)
        second_after = await self.db.get_token(self.second.id)
        self.assertTrue(second_after.is_active)
        self.assertEqual("backoff", second_after.auth_state)
        self.assertEqual("oauth_callback_missing", second_after.last_auth_error_class)
        await self._assert_all_released()


if __name__ == "__main__":
    unittest.main()
