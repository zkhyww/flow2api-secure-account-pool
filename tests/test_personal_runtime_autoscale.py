import asyncio
import unittest

from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    ResidentTabInfo,
    _PersonalBrowserPoolService,
)


class PersonalRuntimeAutoscaleTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _pool_with_cold_workers(worker_count):
        pool = _PersonalBrowserPoolService()
        pool._workers = [
            BrowserCaptchaService(
                browser_instance_id=index + 1,
                max_resident_tabs_override=1,
            )
            for index in range(worker_count)
        ]
        pool._worker_tab_limits = [1] * worker_count
        return pool

    async def test_three_configured_slots_exist_without_starting_browser_processes(self):
        pool = self._pool_with_cold_workers(3)

        self.assertEqual([1, 1, 1], pool._build_worker_tab_limits(1, 3))
        self.assertEqual(3, len(pool._workers))
        self.assertTrue(all(worker.browser is None for worker in pool._workers))

    async def test_dense_dispatch_prefers_worker_with_reusable_resident_capacity(self):
        pool = self._pool_with_cold_workers(3)
        warm_worker = pool._workers[0]
        warm_worker._initialized = True
        warm_worker.browser = type("Browser", (), {"stopped": False})()
        resident = ResidentTabInfo(
            tab=object(),
            slot_id="b1-slot-warm",
            project_id="project-a",
            token_id=1,
        )
        resident.recaptcha_ready = True
        warm_worker._resident_tabs[resident.slot_id] = resident

        worker_index, selected = await pool._acquire_worker(
            project_id="project-b",
            token_id=2,
            ensure_workers=False,
            allow_affinity=False,
        )
        await pool._release_worker_reservation(worker_index)

        self.assertEqual(0, worker_index)
        self.assertIs(warm_worker, selected)
        self.assertIsNone(pool._workers[1].browser)
        self.assertIsNone(pool._workers[2].browser)

    async def test_pressure_uses_at_most_ten_workers_and_queues_the_next_request(self):
        pool = self._pool_with_cold_workers(10)
        acquired = []
        for token_id in range(1, 11):
            worker_index, _worker = await pool._acquire_worker(
                project_id=f"project-{token_id}",
                token_id=token_id,
                ensure_workers=False,
                allow_affinity=False,
            )
            acquired.append(worker_index)

        overflow = asyncio.create_task(
            pool._acquire_worker(
                project_id="project-overflow",
                token_id=11,
                ensure_workers=False,
                allow_affinity=False,
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(overflow.done())

        await pool._release_worker_reservation(acquired[0])
        overflow_index, _worker = await asyncio.wait_for(overflow, timeout=0.5)
        await pool._release_worker_reservation(overflow_index)
        for worker_index in acquired[1:]:
            await pool._release_worker_reservation(worker_index)

        self.assertEqual(set(range(10)), set(acquired))
        self.assertEqual(10, len(pool._workers))
        self.assertEqual({}, pool._worker_dispatch_reservations)


if __name__ == "__main__":
    unittest.main()
