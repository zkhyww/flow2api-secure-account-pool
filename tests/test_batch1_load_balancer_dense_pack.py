import unittest
from unittest.mock import AsyncMock, patch

from src.core.account_tiers import PAYGATE_TIER_NOT_PAID, PAYGATE_TIER_TWO
from src.core.config import config
from src.services.concurrency_manager import ConcurrencyManager
from src.services.load_balancer import LoadBalancer


class FakeToken:
    def __init__(self, token_id, tier=PAYGATE_TIER_NOT_PAID, enabled=True, invalid_at=False):
        self.id = token_id
        self.email = f"fake-{token_id}"
        self.image_enabled = enabled
        self.video_enabled = enabled
        self.is_active = enabled
        self.user_paygate_tier = tier
        self.at = None if invalid_at else "valid-at"
        self.at_expires = None
        self.credits = 100
        self.extension_route_key = None
        self.image_concurrency = -1
        self.video_concurrency = -1
        self.ban_reason = None


class FakeTokenManager:
    def __init__(self, tokens):
        self.tokens = tokens
        self.db = None

    async def get_active_tokens(self):
        return list(self.tokens)

    def needs_at_refresh(self, token):
        return False

    async def ensure_valid_token(self, token):
        return None if token.at is None else token


class LoadBalancerDensePackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_mode = config.call_logic_mode
        self.old_captcha = config.captcha_method
        config.set_call_logic_mode("default")
        config.set_captcha_method("yescaptcha")

    async def asyncTearDown(self):
        config.set_call_logic_mode(self.old_mode)
        config.set_captcha_method(self.old_captcha)

    async def test_idle_prefers_lowest_token_id(self):
        cm = ConcurrencyManager()
        await cm.initialize([FakeToken(1), FakeToken(2)])
        lb = LoadBalancer(FakeTokenManager([FakeToken(1), FakeToken(2)]), cm)
        self.assertEqual((await lb.select_token(for_image_generation=True)).id, 1)

    async def test_existing_capacity_keeps_first_account(self):
        tokens = [FakeToken(1), FakeToken(2)]
        cm = ConcurrencyManager(); await cm.initialize(tokens)
        await cm.acquire_image(1)
        lb = LoadBalancer(FakeTokenManager(tokens), cm)
        self.assertEqual((await lb.select_token(for_image_generation=True)).id, 1)

    async def test_full_or_cooldown_moves_to_second(self):
        tokens = [FakeToken(1), FakeToken(2)]
        cm = ConcurrencyManager(); await cm.initialize(tokens)
        for _ in range(3):
            await cm.acquire_image(1)
        lb = LoadBalancer(FakeTokenManager(tokens), cm)
        self.assertEqual((await lb.select_token(for_image_generation=True)).id, 2)
        await cm.release_image(1); await cm.release_image(1); await cm.release_image(1)
        await cm.record_rate_limit(1, "image")
        self.assertEqual((await lb.select_token(for_image_generation=True)).id, 2)

    async def test_filters_disabled_tier_and_invalid_at(self):
        tokens = [FakeToken(1, enabled=False), FakeToken(2)]
        cm = ConcurrencyManager(); await cm.initialize(tokens)
        self.assertEqual((await LoadBalancer(FakeTokenManager(tokens), cm).select_token(for_image_generation=True)).id, 2)
        tokens = [FakeToken(1), FakeToken(2, tier=PAYGATE_TIER_TWO)]
        tokens[0].at = None
        self.assertEqual((await LoadBalancer(FakeTokenManager(tokens), cm).select_token(for_image_generation=True, model="fixture-4k")).id, 2)

    async def test_polling_round_robin_and_scale_no_truncation(self):
        old = config.call_logic_mode
        config.set_call_logic_mode("polling")
        try:
            tokens = [FakeToken(i) for i in range(1, 3)]
            lb = LoadBalancer(FakeTokenManager(tokens), ConcurrencyManager())
            self.assertEqual((await lb.select_token()).id, 1)
            self.assertEqual((await lb.select_token()).id, 2)
            for count in (0, 1, 200, 201, 500):
                result = await LoadBalancer(FakeTokenManager([FakeToken(i) for i in range(count)]), ConcurrencyManager()).select_token()
                if count:
                    self.assertIsNotNone(result)
        finally:
            config.set_call_logic_mode(old)

    async def test_reserve_pending_release_no_leak(self):
        tokens = [FakeToken(1)]
        cm = ConcurrencyManager(); await cm.initialize(tokens)
        lb = LoadBalancer(FakeTokenManager(tokens), cm)
        token = await lb.select_token(for_image_generation=True, reserve=True, track_pending=True)
        self.assertEqual(await cm.get_image_inflight(token.id), 1)
        self.assertEqual(await lb._get_pending_count(token.id, True, False), 1)
        await cm.release_image(token.id)
        await lb.release_pending(token.id, for_image_generation=True)
        self.assertEqual(await cm.get_image_inflight(token.id), 0)
        self.assertEqual(await lb._get_pending_count(token.id, True, False), 0)

    async def test_all_disconnected_extension_routes_report_public_repair_guidance(self):
        """A route outage must not be presented as an anonymous no-token condition."""
        config.set_captcha_method("extension")
        tokens = [FakeToken(1), FakeToken(2)]
        service = type("DisconnectedExtension", (), {
            "has_connection_for_token": AsyncMock(side_effect=[(False, "private-a"), (False, "private-b")]),
            "describe_routes": lambda self: "",
        })()
        with patch(
            "src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance",
            new=AsyncMock(return_value=service),
        ):
            balancer = LoadBalancer(FakeTokenManager(tokens), ConcurrencyManager())
            self.assertIsNone(await balancer.select_token(for_image_generation=True))
            reason = await balancer.get_unavailable_reason(for_image_generation=True)

        self.assertEqual(
            "extension_not_connected: 插件未连接或需要重新配对",
            reason,
        )
        self.assertNotIn("private-", reason)

    async def test_connected_extension_candidate_does_not_report_global_disconnect(self):
        """One connected eligible route is sufficient; do not emit a global outage message."""
        config.set_captcha_method("extension")
        tokens = [FakeToken(1), FakeToken(2)]
        service = type("PartiallyConnectedExtension", (), {
            "has_connection_for_token": AsyncMock(side_effect=[(False, "private-a"), (True, "private-b"), (False, "private-a"), (True, "private-b")]),
            "describe_routes": lambda self: "",
        })()
        with patch(
            "src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance",
            new=AsyncMock(return_value=service),
        ):
            balancer = LoadBalancer(FakeTokenManager(tokens), ConcurrencyManager())
            selected = await balancer.select_token(for_image_generation=True)
            reason = await balancer.get_unavailable_reason(for_image_generation=True)

        self.assertEqual(2, selected.id)
        self.assertIsNone(reason)
