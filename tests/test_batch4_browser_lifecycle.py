import asyncio
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src import main
from src.core.database import Database
from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    ResidentTabInfo,
    _PersonalBrowserPoolService,
)
from src.services.token_manager import TokenManager


class _FakeBrowserProcess:
    def __init__(self):
        self.stopped = False
        self.targets = []

    def stop(self):
        self.stopped = True


class _FakeFlowClient:
    async def st_to_at(self, _session_value):
        return {
            "access_token": "access-placeholder",
            "expires": "2030-01-01T00:00:00Z",
            "user": {},
        }

    async def get_credits(self, _access_value):
        return {"credits": 1, "userPaygateTier": "PAYGATE_TIER_ONE"}


class Batch4BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _make_pool(self, worker_count: int, tabs_per_worker: int = 2):
        pool = _PersonalBrowserPoolService()
        pool._ensure_idle_worker_reaper = AsyncMock()
        pool._is_token_pool_enabled = Mock(return_value=False)
        with patch.object(
            BrowserCaptchaService,
            "_resolve_configured_browser_count",
            return_value=worker_count,
        ), patch.object(
            pool,
            "_resolve_worker_resident_tabs",
            return_value=tabs_per_worker,
        ):
            await pool._ensure_workers()
        # Keep the configured lightweight workers stable while exercising dispatch.
        # Individual tests cover reload/shrink through the existing Batch 1 suite.
        pool._ensure_workers = AsyncMock()
        return pool

    @staticmethod
    def _install_page_boundary(pool, *, block_event=None, entered_event=None, bindings=None):
        bindings = bindings if bindings is not None else []

        for worker_index, worker in enumerate(pool._workers):
            async def fake_get_token(
                self,
                project_id,
                action="IMAGE_GENERATION",
                token_id=None,
                *,
                return_slot_id=False,
                _worker_index=worker_index,
            ):
                if not self._initialized or not self.browser or self.browser.stopped:
                    self._initialized = True
                    self.browser = _FakeBrowserProcess()
                slot_id = f"b{_worker_index + 1}-slot-{token_id or 'anonymous'}"
                resident = self._resident_tabs.get(slot_id)
                if resident is None:
                    resident = ResidentTabInfo(
                        tab=object(),
                        slot_id=slot_id,
                        project_id=project_id,
                        token_id=token_id,
                    )
                    resident.recaptcha_ready = True
                    self._resident_tabs[slot_id] = resident
                self._project_resident_affinity[str(project_id)] = slot_id
                if token_id is not None:
                    self._token_resident_affinity[str(token_id)] = slot_id
                bindings.append((_worker_index, token_id, project_id, slot_id))
                if entered_event is not None:
                    entered_event.set()
                if block_event is not None:
                    await block_event.wait()
                result = "opaque-page-result"
                return (result, slot_id) if return_slot_id else result

            worker.get_token = types.MethodType(fake_get_token, worker)
        return bindings

    @staticmethod
    async def _close_pool(pool):
        for worker in list(pool._workers):
            worker.browser = None
            worker._initialized = False
            worker._resident_tabs.clear()
        await pool.close()

    async def test_pool_configuration_and_account_import_do_not_launch_processes(self):
        process_boundary = AsyncMock()
        pool = await self._make_pool(3)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(str(Path(tmp) / "lazy-account.db"))
                await db.init_db()
                manager = TokenManager(db, _FakeFlowClient())
                with patch.object(
                    BrowserCaptchaService,
                    "initialize",
                    new=process_boundary,
                ), patch.object(manager, "_get_project_pool_size", return_value=1):
                    await manager.add_token(
                        st="session-placeholder",
                        project_id="project-a",
                    )

            self.assertEqual(3, len(pool._workers))
            self.assertTrue(all(worker.browser is None for worker in pool._workers))
            process_boundary.assert_not_awaited()
        finally:
            await self._close_pool(pool)

    async def test_application_startup_with_accounts_does_not_warm_or_launch_browser(self):
        process_starts = []
        pool = _PersonalBrowserPoolService()
        pool._ensure_idle_worker_reaper = AsyncMock()
        pool._is_token_pool_enabled = Mock(return_value=False)

        async def fake_page_boundary(worker, project_id, **_kwargs):
            process_starts.append((worker._browser_instance_id, project_id))
            return f"b{worker._browser_instance_id}-warm", object()

        async def recover():
            return None

        fake_db = SimpleNamespace(
            db_exists=Mock(return_value=True),
            init_db=AsyncMock(),
            check_and_migrate_db=AsyncMock(),
            reload_config_to_memory=AsyncMock(),
            get_captcha_config=AsyncMock(return_value=SimpleNamespace(captcha_method="personal")),
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
            get_all_tokens=AsyncMock(return_value=[SimpleNamespace(id=1)]),
            get_personal_warmup_project_ids=AsyncMock(return_value=["project-a"]),
            start_protocol_refresher=Mock(),
            stop_protocol_refresher=AsyncMock(),
            auto_unban_429_tokens=AsyncMock(),
        )
        fake_config = SimpleNamespace(
            get_raw_config=Mock(return_value={}),
            cache_timeout=0,
            cache_enabled=False,
            captcha_method="personal",
            browser_count=2,
            personal_max_resident_tabs=2,
            server_host="127.0.0.1",
            server_port=8000,
        )
        stale_runtime_cleanup = AsyncMock(return_value={})

        with patch.object(main, "db", fake_db), patch.object(
            main,
            "generation_handler",
            fake_handler,
        ), patch.object(main, "token_manager", fake_token_manager), patch.object(
            main,
            "concurrency_manager",
            SimpleNamespace(initialize=AsyncMock()),
        ), patch.object(main, "config", fake_config), patch.object(
            BrowserCaptchaService,
            "cleanup_stale_runtime_artifacts",
            new=stale_runtime_cleanup,
        ), patch.object(
            BrowserCaptchaService,
            "get_instance",
            new=AsyncMock(return_value=pool),
        ), patch.object(
            BrowserCaptchaService,
            "_resolve_configured_browser_count",
            return_value=2,
        ), patch.object(
            BrowserCaptchaService,
            "_ensure_resident_tab",
            new=fake_page_boundary,
        ), patch("builtins.print"):
            async with main.lifespan(None):
                self.assertEqual([], process_starts)
                stale_runtime_cleanup.assert_awaited_once_with(reason="startup")

    async def test_application_startup_cleanup_failure_does_not_block_personal_service(self):
        fake_db = SimpleNamespace(
            db_exists=Mock(return_value=True),
            init_db=AsyncMock(),
            check_and_migrate_db=AsyncMock(),
            reload_config_to_memory=AsyncMock(),
            get_captcha_config=AsyncMock(return_value=SimpleNamespace(captcha_method="personal")),
        )
        fake_file_cache = SimpleNamespace(
            set_timeout=Mock(),
            refresh_cleanup_task=AsyncMock(return_value=False),
            stop_cleanup_task=AsyncMock(),
        )
        fake_handler = SimpleNamespace(
            file_cache=fake_file_cache,
            recover_incomplete_tasks=AsyncMock(return_value=None),
        )
        fake_token_manager = SimpleNamespace(
            get_all_tokens=AsyncMock(return_value=[]),
            start_protocol_refresher=Mock(),
            stop_protocol_refresher=AsyncMock(),
            auto_unban_429_tokens=AsyncMock(),
        )
        fake_config = SimpleNamespace(
            get_raw_config=Mock(return_value={}),
            cache_timeout=0,
            cache_enabled=False,
            captcha_method="personal",
            server_host="127.0.0.1",
            server_port=8000,
        )
        cleanup = AsyncMock(side_effect=RuntimeError("synthetic cleanup failure"))
        browser_service = SimpleNamespace(close=AsyncMock())
        get_instance = AsyncMock(return_value=browser_service)

        with patch.object(main, "db", fake_db), patch.object(
            main,
            "generation_handler",
            fake_handler,
        ), patch.object(main, "token_manager", fake_token_manager), patch.object(
            main,
            "concurrency_manager",
            SimpleNamespace(initialize=AsyncMock()),
        ), patch.object(main, "config", fake_config), patch.object(
            BrowserCaptchaService,
            "cleanup_stale_runtime_artifacts",
            new=cleanup,
        ), patch.object(
            BrowserCaptchaService,
            "get_instance",
            new=get_instance,
        ), patch.object(
            main.yingce_adapter,
            "shutdown_background_video_tasks",
            new=AsyncMock(),
        ), patch("builtins.print"):
            async with main.lifespan(None):
                cleanup.assert_awaited_once_with(reason="startup")
                get_instance.assert_awaited_once_with(fake_db)

    async def test_first_task_starts_only_one_browser(self):
        pool = await self._make_pool(3)
        self._install_page_boundary(pool)
        try:
            result, slot_id = await pool.get_token(
                "project-a",
                token_id=1,
                return_slot_id=True,
            )

            live_workers = [worker for worker in pool._workers if worker.browser is not None]
            self.assertEqual("opaque-page-result", result)
            self.assertTrue(slot_id.startswith("b1-"))
            self.assertEqual(1, len(live_workers))
        finally:
            await self._close_pool(pool)

    async def test_same_token_project_concurrency_reuses_worker_while_tab_capacity_exists(self):
        pool = await self._make_pool(2, tabs_per_worker=2)
        release = asyncio.Event()
        first_entered = asyncio.Event()
        bindings = self._install_page_boundary(
            pool,
            block_event=release,
            entered_event=first_entered,
        )
        first = asyncio.create_task(pool.get_token("project-a", token_id=1, return_slot_id=True))
        second = None
        try:
            await asyncio.wait_for(first_entered.wait(), timeout=0.5)
            second = asyncio.create_task(pool.get_token("project-a", token_id=1, return_slot_id=True))
            for _ in range(20):
                if len(bindings) >= 2:
                    break
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

            self.assertEqual(2, len(bindings))
            self.assertEqual(bindings[0][0], bindings[1][0])
        finally:
            release.set()
            if second is not None:
                await asyncio.gather(first, second, return_exceptions=True)
            else:
                await asyncio.gather(first, return_exceptions=True)
            await self._close_pool(pool)

    async def test_same_account_at_tab_capacity_uses_next_existing_worker(self):
        pool = await self._make_pool(2, tabs_per_worker=1)
        release = asyncio.Event()
        first_entered = asyncio.Event()
        bindings = self._install_page_boundary(
            pool,
            block_event=release,
            entered_event=first_entered,
        )
        first = asyncio.create_task(pool.get_token("project-a", token_id=1, return_slot_id=True))
        second = None
        try:
            await asyncio.wait_for(first_entered.wait(), timeout=0.5)
            second = asyncio.create_task(pool.get_token("project-a", token_id=1, return_slot_id=True))
            for _ in range(20):
                if len(bindings) >= 2:
                    break
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

            self.assertEqual(2, len(bindings))
            self.assertNotEqual(bindings[0][0], bindings[1][0])
        finally:
            release.set()
            if second is not None:
                await asyncio.gather(first, second, return_exceptions=True)
            else:
                await asyncio.gather(first, return_exceptions=True)
            await self._close_pool(pool)

    async def test_full_or_cooling_worker_activates_next_worker_and_keeps_bindings_isolated(self):
        pool = await self._make_pool(2, tabs_per_worker=1)
        release = asyncio.Event()
        first_entered = asyncio.Event()
        bindings = self._install_page_boundary(
            pool,
            block_event=release,
            entered_event=first_entered,
        )
        first = asyncio.create_task(pool.get_token("project-a", token_id=1, return_slot_id=True))
        second = None
        try:
            await asyncio.wait_for(first_entered.wait(), timeout=0.5)
            second = asyncio.create_task(pool.get_token("project-b", token_id=2, return_slot_id=True))
            for _ in range(20):
                if len(bindings) >= 2:
                    break
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

            self.assertEqual({0, 1}, {item[0] for item in bindings[:2]})
            self.assertEqual(
                {(1, "project-a"), (2, "project-b")},
                {(item[1], item[2]) for item in bindings[:2]},
            )
            for worker_index, token_id, project_id, slot_id in bindings[:2]:
                worker = pool._workers[worker_index]
                self.assertEqual(slot_id, worker._token_resident_affinity[str(token_id)])
                self.assertEqual(slot_id, worker._project_resident_affinity[project_id])

            pool._project_worker_affinity["project-c"] = 0
            pool._token_worker_affinity["3"] = 0
            pool._workers[0]._token_resident_affinity["3"] = "b1-slot-3"
            pool._workers[0]._browser_launch_cooldown_until = time.monotonic() + 60
            pool._workers[0]._initialized = False
            pool._workers[0].browser = None
            acquired_index, _ = await pool._acquire_worker(
                project_id="project-c",
                token_id=3,
                ensure_workers=False,
            )
            await pool._release_worker_reservation(acquired_index)
            self.assertEqual(1, acquired_index)
        finally:
            release.set()
            if second is not None:
                await asyncio.gather(first, second, return_exceptions=True)
            else:
                await asyncio.gather(first, return_exceptions=True)
            await self._close_pool(pool)

    async def test_idle_runtime_closes_busy_runtime_survives_and_later_task_restarts(self):
        worker = BrowserCaptchaService(browser_instance_id=1, max_resident_tabs_override=1)
        worker._initialized = True
        worker.browser = _FakeBrowserProcess()
        worker._runtime_last_active_at = time.time() - 600

        async def fake_shutdown(*_args, **_kwargs):
            worker.browser = None
            worker._initialized = False

        worker._shutdown_browser_runtime = AsyncMock(side_effect=fake_shutdown)
        async with worker._tab_build_lock:
            busy_result = await worker.shutdown_idle_runtime_if_needed(idle_ttl_seconds=60)
        self.assertFalse(busy_result)
        worker._shutdown_browser_runtime.assert_not_awaited()

        idle_result = await worker.shutdown_idle_runtime_if_needed(idle_ttl_seconds=60)
        self.assertTrue(idle_result)
        self.assertIsNone(worker.browser)

        pool = _PersonalBrowserPoolService()
        pool._workers = [worker]
        pool._ensure_workers = AsyncMock()
        self._install_page_boundary(pool)
        try:
            result = await pool.get_token("project-a", token_id=1)
            self.assertEqual("opaque-page-result", result)
            self.assertIsNotNone(worker.browser)
        finally:
            worker.browser = None
            worker._initialized = False
            await pool.close()

    async def test_idle_tab_reaper_requests_runtime_shutdown_after_last_tab_closes(self):
        worker = BrowserCaptchaService(browser_instance_id=1, max_resident_tabs_override=1)
        worker._initialized = True
        worker.browser = _FakeBrowserProcess()
        worker._idle_tab_ttl_seconds = 60
        resident = ResidentTabInfo(
            tab=object(),
            slot_id="slot-stale",
            project_id="project-a",
            token_id=1,
        )
        resident.last_used_at = time.time() - 120
        worker._resident_tabs["slot-stale"] = resident

        async def close_resident(slot_id):
            worker._resident_tabs.pop(slot_id, None)

        sleep_calls = 0

        async def one_reaper_tick(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError

        worker._close_resident_tab = AsyncMock(side_effect=close_resident)
        worker.shutdown_idle_runtime_if_needed = AsyncMock(return_value=True)

        with patch(
            "src.services.browser_captcha_personal.asyncio.sleep",
            new=one_reaper_tick,
        ):
            await worker._idle_tab_reaper_loop()

        worker._close_resident_tab.assert_awaited_once_with("slot-stale")
        worker.shutdown_idle_runtime_if_needed.assert_awaited_once()
        self.assertEqual({}, worker._resident_tabs)

    async def test_process_cap_queues_excess_without_globally_serializing_available_workers(self):
        pool = _PersonalBrowserPoolService()
        pool._workers = [
            BrowserCaptchaService(browser_instance_id=index + 1, max_resident_tabs_override=1)
            for index in range(10)
        ]

        first_index, _ = await pool._acquire_worker(project_id="project-a", ensure_workers=False)
        second_index, _ = await pool._acquire_worker(project_id="project-b", ensure_workers=False)
        self.assertNotEqual(first_index, second_index)
        await pool._release_worker_reservation(first_index)
        await pool._release_worker_reservation(second_index)

        pool._worker_dispatch_reservations = {index: 1 for index in range(10)}
        queued_acquire = asyncio.create_task(
            pool._acquire_worker(project_id="project-overflow", ensure_workers=False)
        )
        await asyncio.sleep(0)
        was_queued = not queued_acquire.done()

        if was_queued:
            await pool._release_worker_reservation(0)
            acquired_index, _ = await asyncio.wait_for(queued_acquire, timeout=0.5)
        else:
            acquired_index, _ = queued_acquire.result()
        await pool._release_worker_reservation(acquired_index)

        self.assertTrue(was_queued)
        self.assertLessEqual(len(pool._workers), 10)

    async def test_cancel_timeout_exception_and_repeated_release_do_not_leak_reservations(self):
        pool = await self._make_pool(2, tabs_per_worker=1)
        try:
            entered = asyncio.Event()
            never = asyncio.Event()

            async def blocking_get_token(self, *_args, **_kwargs):
                entered.set()
                await never.wait()

            for worker in pool._workers:
                worker.get_token = types.MethodType(blocking_get_token, worker)

            cancelled = asyncio.create_task(pool.get_token("project-cancel", token_id=1))
            await asyncio.wait_for(entered.wait(), timeout=0.5)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            self.assertEqual({}, pool._worker_dispatch_reservations)

            entered.clear()
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    pool.get_token("project-timeout", token_id=2),
                    timeout=0.01,
                )
            self.assertEqual({}, pool._worker_dispatch_reservations)

            async def failing_get_token(self, *_args, **_kwargs):
                raise RuntimeError("page-boundary-failure")

            for worker in pool._workers:
                worker.get_token = types.MethodType(failing_get_token, worker)
            self.assertIsNone(await pool.get_token("project-error", token_id=3))
            self.assertEqual({}, pool._worker_dispatch_reservations)

            await pool._release_worker_reservation(0)
            await pool._release_worker_reservation(0)
            self.assertEqual({}, pool._worker_dispatch_reservations)
        finally:
            await self._close_pool(pool)


if __name__ == "__main__":
    unittest.main()
