import unittest

from src.services.compat_video_tasks import (
    CompatVideoTaskRegistry,
    VideoTaskCapacityError,
)


class CompatVideoTaskRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_task_expires_and_idempotency_slot_can_be_reused(self):
        now = [1000.0]
        registry = CompatVideoTaskRegistry(
            ttl_seconds=10,
            capacity=4,
            clock=lambda: now[0],
        )
        first, reused = await registry.create_idempotent(
            model="omni",
            size="1792x1024",
            seconds=10,
            idempotency_digest="synthetic-key-digest",
            request_fingerprint="synthetic-request-fingerprint",
        )
        self.assertFalse(reused)
        await registry.update(first.id, status="completed", progress=100)

        now[0] = 1011.0
        self.assertIsNone(await registry.get(first.id))

        second, reused = await registry.create_idempotent(
            model="omni",
            size="1792x1024",
            seconds=10,
            idempotency_digest="synthetic-key-digest",
            request_fingerprint="changed-after-expiry",
        )
        self.assertFalse(reused)
        self.assertNotEqual(first.id, second.id)

    async def test_ttl_expiry_cannot_be_overwritten_by_same_update_to_completed(self):
        now = [1500.0]
        registry = CompatVideoTaskRegistry(
            ttl_seconds=1,
            capacity=1,
            clock=lambda: now[0],
        )
        task = await registry.create(model="omni", size=None, seconds=10)
        await registry.update(task.id, status="in_progress", progress=10)

        now[0] = 1502.0
        updated = await registry.update(
            task.id,
            status="completed",
            progress=100,
            filename="synthetic.mp4",
        )

        self.assertIsNotNone(updated)
        self.assertEqual("failed", updated.status)
        self.assertEqual("task_timeout", updated.error_code)
        self.assertIsNone(updated.filename)

    async def test_capacity_evicts_oldest_terminal_task_before_rejecting(self):
        now = [2000.0]
        registry = CompatVideoTaskRegistry(
            ttl_seconds=100,
            capacity=2,
            clock=lambda: now[0],
        )
        first = await registry.create(model="omni", size=None, seconds=10)
        second = await registry.create(model="omni", size=None, seconds=10)
        await registry.update(first.id, status="completed", progress=100)

        now[0] = 2001.0
        third = await registry.create(model="omni", size=None, seconds=10)

        self.assertIsNone(await registry.get(first.id))
        self.assertIsNotNone(await registry.get(second.id))
        self.assertIsNotNone(await registry.get(third.id))

    async def test_capacity_never_evicts_active_tasks(self):
        registry = CompatVideoTaskRegistry(capacity=2)
        first = await registry.create(model="omni", size=None, seconds=10)
        second = await registry.create(model="omni", size=None, seconds=10)

        with self.assertRaises(VideoTaskCapacityError):
            await registry.create(model="omni", size=None, seconds=10)

        self.assertIsNotNone(await registry.get(first.id))
        self.assertIsNotNone(await registry.get(second.id))


if __name__ == "__main__":
    unittest.main()
