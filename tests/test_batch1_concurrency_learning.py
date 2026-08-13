import unittest

from src.services.concurrency_manager import ConcurrencyManager


class _Clock:
    def __init__(self):
        self.value = 1000.0

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Batch1ConcurrencyLearningTests(unittest.IsolatedAsyncioTestCase):
    async def _init_unknown(self, manager=None):
        manager = manager or self.manager
        token = type("Token", (), {"id": 1, "image_concurrency": -1, "video_concurrency": -1})()
        await manager.initialize([token])
        return manager

    async def asyncSetUp(self):
        self.manager = ConcurrencyManager()

    async def test_unknown_image_and_video_start_at_three(self):
        await self._init_unknown()
        for media in ("image", "video"):
            acquire = getattr(self.manager, f"acquire_{media}")
            can_use = getattr(self.manager, f"can_use_{media}")
            remaining = getattr(self.manager, f"get_{media}_remaining")
            release = getattr(self.manager, f"release_{media}")
            self.assertEqual(await remaining(1), 3)
            self.assertTrue(await acquire(1))
            self.assertTrue(await acquire(1))
            self.assertTrue(await acquire(1))
            self.assertFalse(await can_use(1))
            self.assertFalse(await acquire(1))
            self.assertEqual(await remaining(1), 0)
            await release(1)
            await release(1)
            await release(1)
            self.assertEqual(await remaining(1), 3)

    async def test_reset_and_remove_clean_learning_state(self):
        await self._init_unknown()
        await self.manager.reset_token(1, image_concurrency=3, video_concurrency=2)
        await self.manager.record_success(1, "image")
        await self.manager.record_success(1, "image")
        self.assertTrue(await self.manager.acquire_image(1))
        self.assertTrue(await self.manager.acquire_image(1))
        self.assertTrue(await self.manager.acquire_image(1))
        await self.manager.release_image(1)
        await self.manager.release_image(1)
        await self.manager.release_image(1)
        await self.manager.reset_token(1, image_concurrency=-1, video_concurrency=-1)
        self.assertEqual(await self.manager.get_image_remaining(1), 3)
        await self.manager.remove_token(1)
        await self._init_unknown()
        self.assertEqual(await self.manager.get_image_remaining(1), 3)

    async def test_learning_success_and_429_cooldown(self):
        clock = _Clock()
        manager = ConcurrencyManager(clock=clock.now)
        await self._init_unknown(manager)
        for _ in range(3):
            await manager.record_success(1, "image")
        self.assertTrue(await manager.acquire_image(1))
        self.assertTrue(await manager.acquire_image(1))
        self.assertTrue(await manager.acquire_image(1))
        self.assertTrue(await manager.acquire_image(1))
        self.assertTrue(await manager.acquire_image(1))
        self.assertTrue(await manager.acquire_image(1))
        await manager.release_image(1)
        await manager.release_image(1)
        await manager.release_image(1)
        await manager.release_image(1)
        await manager.release_image(1)
        await manager.release_image(1)
        await manager.record_rate_limit(1, "image", cooldown_seconds=10)
        self.assertFalse(await manager.can_use_image(1))
        clock.advance(11)
        self.assertTrue(await manager.can_use_image(1))

    async def test_cooldown_is_symmetric_and_initialize_clears_it(self):
        clock = _Clock()
        manager = ConcurrencyManager(clock=clock.now)
        await self._init_unknown(manager)
        await manager.record_rate_limit(1, "image")
        await manager.record_rate_limit(1, "video")
        self.assertFalse(await manager.can_use_image(1))
        self.assertFalse(await manager.can_use_video(1))
        await manager.initialize([type("Token", (), {"id": 1, "image_concurrency": -1, "video_concurrency": -1})()])
        self.assertTrue(await manager.can_use_image(1))
        self.assertTrue(await manager.can_use_video(1))

    async def test_explicit_limit_is_learning_ceiling_not_fixed_limit(self):
        manager = ConcurrencyManager()
        await manager.initialize([type("Token", (), {"id": 1, "image_concurrency": 2, "video_concurrency": 2})()])
        self.assertTrue(await manager.acquire_image(1))
        self.assertTrue(await manager.acquire_image(1))
        self.assertFalse(await manager.acquire_image(1))
        await manager.release_image(1)
        await manager.release_image(1)
        await manager.record_success(1, "image")
        self.assertTrue(await manager.acquire_image(1))
        self.assertTrue(await manager.acquire_image(1))
        self.assertFalse(await manager.acquire_image(1))
        await manager.record_rate_limit(1, "image")
        self.assertFalse(await manager.can_use_image(1))

    async def test_unknown_learning_can_exceed_ten(self):
        manager = ConcurrencyManager()
        await self._init_unknown(manager)
        for _ in range(8):
            await manager.record_success(1, "image")
        self.assertGreaterEqual(await manager.get_image_remaining(1), 11)

    async def test_invalid_media_type_rejected(self):
        await self._init_unknown()
        with self.assertRaises(ValueError):
            await self.manager.record_success(1, "audio")
        with self.assertRaises(ValueError):
            await self.manager.record_rate_limit(1, "audio")


if __name__ == "__main__":
    unittest.main()
