import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.account_tiers import PAYGATE_TIER_NOT_PAID, PAYGATE_TIER_TWO
from src.core.config import config
from src.core.database import Database
from src.core.models import Project, Token
from src.services.concurrency_manager import ConcurrencyManager
from src.services.generation_handler import GenerationHandler
from src.services.load_balancer import LoadBalancer
from src.services.proxy_manager import ProxyManager
from src.services.token_manager import TokenManager


IMAGE_MODEL = "gemini-3.1-flash-image-landscape"
TIER_TWO_IMAGE_MODEL = "gemini-3.0-pro-image-landscape-4k"


class _FlowBoundaryError(Exception):
    def __init__(self, *, status_code, error_code):
        super().__init__(error_code)
        self.status_code = status_code
        self.error_code = error_code


class _RoutingFlowClient:
    def __init__(self):
        self.submit_token_ids = []
        self.failures_by_token = {}

    def clear_request_fingerprint(self):
        return None

    async def prefill_remote_browser_pool(self, **kwargs):
        return None

    async def generate_image(self, **kwargs):
        token_id = kwargs.get("token_id")
        self.submit_token_ids.append(token_id)
        failure = self.failures_by_token.get(token_id)
        if failure is not None:
            raise failure
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


async def _collect(handler, *, model=IMAGE_MODEL, key):
    chunks = []
    async for chunk in handler.handle_generation(
        model=model,
        prompt="",
        images=None,
        stream=False,
        idempotency_key=key,
    ):
        chunks.append(chunk)
    return chunks


class Batch3FailureRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_captcha = config.captcha_method
        self.old_call_mode = config.call_logic_mode
        self.old_cache_enabled = config.cache_enabled
        config.set_captcha_method("yescaptcha")
        config.set_call_logic_mode("default")
        config.set_cache_enabled(False)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "batch3-routing.db"))
        await self.db.init_db()
        await self.db.init_config_from_toml(config.get_raw_config(), is_first_startup=True)
        self.flow = _RoutingFlowClient()

    async def asyncTearDown(self):
        config.set_captcha_method(self.old_captcha)
        config.set_call_logic_mode(self.old_call_mode)
        config.set_cache_enabled(self.old_cache_enabled)
        self.temp_dir.cleanup()

    async def _add_token(self, *, credits, ordinal, tier=PAYGATE_TIER_NOT_PAID):
        token_id = await self.db.add_token(
            Token(
                st=f"fixture-session-{ordinal}",
                at=None,
                at_expires=datetime.now(timezone.utc) + timedelta(hours=2),
                email="",
                credits=credits,
                user_paygate_tier=tier,
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

    async def _assert_account_local_fault_is_observable(self, token_id):
        token = await self.db.get_token(token_id)
        stats = await self.db.get_token_stats(token_id)
        self.assertTrue(
            (not token.is_active)
            or bool(token.ban_reason)
            or bool(stats and stats.consecutive_error_count > 0)
        )

    async def _assert_authentication_account_is_persistently_excluded(self, token_id):
        token = await self.db.get_token(token_id)
        self.assertTrue(token.is_active)
        self.assertEqual("backoff", token.auth_state)
        self.assertEqual("oauth_callback_missing", token.last_auth_error_class)

    async def _assert_account_is_healthy(self, token_id):
        token = await self.db.get_token(token_id)
        stats = await self.db.get_token_stats(token_id)
        self.assertTrue(token.is_active)
        self.assertFalse(token.ban_reason)
        self.assertEqual(stats.consecutive_error_count if stats else 0, 0)

    def _assert_success_task_owned_by_account_and_quota_settled(self, task, token_id):
        self.assertEqual(task.token_id, token_id)
        self.assertEqual(getattr(task, "quota_state", None), "settled")
        self.assertEqual(getattr(task, "quota_reserved", None), 0)

    async def test_zero_credit_first_account_is_never_submitted_and_generation_switches_to_funded_account(self):
        empty = await self._add_token(credits=0, ordinal=1)
        funded = await self._add_token(credits=5, ordinal=2)
        handler, _ = await self._build_handler()
        await _collect(handler, key="quota-route-funded")
        self.assertEqual(self.flow.submit_token_ids, [funded.id])
        self.assertNotIn(empty.id, self.flow.submit_token_ids)

    async def test_all_zero_credit_accounts_fail_safely_without_any_upstream_submit(self):
        await self._add_token(credits=0, ordinal=1)
        await self._add_token(credits=0, ordinal=2)
        handler, _ = await self._build_handler()
        chunks = await _collect(handler, key="quota-route-none")
        self.assertEqual(self.flow.submit_token_ids, [])
        self.assertTrue(chunks)

    async def test_429_account_local_cooldown_routes_once_to_other_healthy_account(self):
        first = await self._add_token(credits=5, ordinal=1)
        second = await self._add_token(credits=5, ordinal=2)
        handler, concurrency_manager = await self._build_handler()
        second_capacity_before = await concurrency_manager.get_image_remaining(second.id)
        self.flow.failures_by_token[first.id] = _FlowBoundaryError(status_code=429, error_code="RATE_LIMITED")
        await _collect(handler, key="route-after-429")
        task = await self.db.get_task_by_idempotency_key("route-after-429")
        self.assertEqual(self.flow.submit_token_ids, [first.id, second.id])
        self.assertFalse(await concurrency_manager.can_use_image(first.id))
        self.assertEqual(await concurrency_manager.get_image_remaining(second.id), second_capacity_before)
        self.assertTrue((await self.db.get_token(first.id)).is_active)
        self.assertTrue((await self.db.get_token(second.id)).is_active)
        self._assert_success_task_owned_by_account_and_quota_settled(task, second.id)

    async def test_authentication_account_local_failure_switches_once_to_second_healthy_account(self):
        first = await self._add_token(credits=5, ordinal=1)
        second = await self._add_token(credits=5, ordinal=2)
        third = await self._add_token(credits=5, ordinal=3)
        handler, _ = await self._build_handler()
        self.flow.failures_by_token[first.id] = _FlowBoundaryError(status_code=401, error_code="AUTHENTICATION")
        await _collect(handler, key="route-auth-failover")
        task = await self.db.get_task_by_idempotency_key("route-auth-failover")
        self.assertEqual(self.flow.submit_token_ids, [first.id, second.id])
        self.assertNotIn(third.id, self.flow.submit_token_ids)
        self.assertEqual(task.status, "succeeded")
        self._assert_success_task_owned_by_account_and_quota_settled(task, second.id)
        await self._assert_authentication_account_is_persistently_excluded(first.id)
        await self._assert_account_is_healthy(second.id)
        await self._assert_account_is_healthy(third.id)

    async def test_all_authentication_failures_try_each_eligible_account_at_most_once_then_stop(self):
        tokens = [
            await self._add_token(credits=5, ordinal=1),
            await self._add_token(credits=5, ordinal=2),
            await self._add_token(credits=5, ordinal=3),
        ]
        handler, _ = await self._build_handler()
        for token in tokens:
            self.flow.failures_by_token[token.id] = _FlowBoundaryError(status_code=401, error_code="AUTHENTICATION")
        await _collect(handler, key="route-auth-all-fail")
        task = await self.db.get_task_by_idempotency_key("route-auth-all-fail")
        expected_ids = [token.id for token in tokens]
        self.assertEqual(self.flow.submit_token_ids, expected_ids)
        self.assertEqual(len(self.flow.submit_token_ids), len(set(self.flow.submit_token_ids)))
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.error_class, "authentication")
        self.assertEqual(getattr(task, "quota_state", None), "released")
        self.assertEqual(getattr(task, "quota_reserved", None), 0)
        for token in tokens:
            await self._assert_authentication_account_is_persistently_excluded(token.id)

    async def test_content_policy_request_global_failure_never_switches_accounts(self):
        first = await self._add_token(credits=5, ordinal=1)
        await self._add_token(credits=5, ordinal=2)
        handler, _ = await self._build_handler()
        self.flow.failures_by_token[first.id] = _FlowBoundaryError(status_code=400, error_code="CONTENT_POLICY")
        await _collect(handler, key="route-policy-stop")
        task = await self.db.get_task_by_idempotency_key("route-policy-stop")
        self.assertEqual(self.flow.submit_token_ids, [first.id])
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.error_class, "content_policy")

    async def test_known_membership_tier_shortage_stops_before_submit_without_account_churn(self):
        await self._add_token(credits=5, ordinal=1, tier=PAYGATE_TIER_NOT_PAID)
        await self._add_token(credits=5, ordinal=2, tier=PAYGATE_TIER_NOT_PAID)
        handler, _ = await self._build_handler()
        chunks = await _collect(handler, model=TIER_TWO_IMAGE_MODEL, key="route-tier-preflight-stop")
        self.assertEqual(self.flow.submit_token_ids, [])
        self.assertTrue(chunks)

    async def test_runtime_membership_account_local_failure_only_tries_other_eligible_tier_account_once(self):
        first_tier_two = await self._add_token(credits=5, ordinal=1, tier=PAYGATE_TIER_TWO)
        second_tier_two = await self._add_token(credits=5, ordinal=2, tier=PAYGATE_TIER_TWO)
        low_tier = await self._add_token(credits=5, ordinal=3, tier=PAYGATE_TIER_NOT_PAID)
        handler, _ = await self._build_handler()
        self.flow.failures_by_token[first_tier_two.id] = _FlowBoundaryError(status_code=403, error_code="MEMBERSHIP_TIER")
        await _collect(handler, model=TIER_TWO_IMAGE_MODEL, key="route-tier-runtime-failover")
        task = await self.db.get_task_by_idempotency_key("route-tier-runtime-failover")
        self.assertEqual(self.flow.submit_token_ids, [first_tier_two.id, second_tier_two.id])
        self.assertNotIn(low_tier.id, self.flow.submit_token_ids)
        self.assertEqual(task.status, "succeeded")
        self._assert_success_task_owned_by_account_and_quota_settled(task, second_tier_two.id)
        await self._assert_account_local_fault_is_observable(first_tier_two.id)
        await self._assert_account_is_healthy(second_tier_two.id)
        await self._assert_account_is_healthy(low_tier.id)


if __name__ == "__main__":
    unittest.main()
