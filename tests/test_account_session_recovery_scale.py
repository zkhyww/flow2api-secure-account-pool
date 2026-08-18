import asyncio
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.database import Database
from src.services.account_profile_store import AccountProfileStore
import src.services.browser_captcha_personal as browser_module
from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    _PersonalBrowserPoolService,
    resolve_effective_browser_count,
)
from src.services.token_manager import TokenManager


class _FakeNodriverConfig:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)

    def __call__(self):
        return list(self.kwargs.get("browser_args") or [])


class AccountSessionRecoveryScaleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._temp_dir.name) / "scale.db"))
        await self.db.init_db()

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    @staticmethod
    def _fake_browser():
        return SimpleNamespace(
            stopped=False,
            targets=[],
            _flow2api_runtime_disconnected=False,
        )

    def _prepare_browser_service(self, service: BrowserCaptchaService) -> BrowserCaptchaService:
        service._resolve_personal_proxy = AsyncMock(return_value=(None, None, None, None, None))
        service._build_proxy_config_signature = AsyncMock(return_value="")
        service._requires_virtual_display = lambda: False
        service._get_live_browser_runtime_identity = AsyncMock(
            return_value=("synthetic-user-agent", "synthetic-product")
        )
        service._apply_configured_browser_startup_cookie = AsyncMock()
        service._capture_visible_startup_page = AsyncMock()
        service._idle_tab_reaper_loop = AsyncMock()
        service._stop_browser_process = AsyncMock()
        service._cleanup_runtime_profile_dirs_after_shutdown = AsyncMock()
        service._cancel_background_runtime_tasks = AsyncMock()
        service._get_browser_process_pid = lambda _browser: None
        service._is_pid_running = lambda _pid: False
        service._should_use_explicit_no_sandbox_retry = lambda _error: False
        return service

    def _make_browser_service(self, label: str) -> BrowserCaptchaService:
        profile_dir = Path(self._temp_dir.name) / f"browser-{label}"
        return self._prepare_browser_service(
            BrowserCaptchaService(
                None,
                force_headed=True,
                persistent_profile_dir=profile_dir,
            )
        )

    @contextmanager
    def _synthetic_nodriver(self, start_side_effect):
        fake_uc = SimpleNamespace(
            Config=_FakeNodriverConfig,
            start=AsyncMock(side_effect=start_side_effect),
        )
        with patch.object(browser_module, "uc", fake_uc), patch.object(
            browser_module,
            "NODRIVER_AVAILABLE",
            True,
        ), patch.object(
            browser_module,
            "DOCKER_HEADED_BLOCKED",
            False,
        ), patch.object(
            browser_module,
            "_patch_nodriver_runtime",
            new=lambda _browser: None,
        ), patch.object(
            browser_module,
            "_resolve_browser_executable_path",
            return_value=(None, "auto"),
        ):
            yield fake_uc.start

    async def _wait_for_initialized_count(
        self,
        services: list[BrowserCaptchaService],
        expected: int,
        *,
        timeout: float = 4.0,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if sum(1 for service in services if service._initialized) >= expected:
                return
            await asyncio.sleep(0.02)
        self.fail(
            f"expected at least {expected} initialized services, got "
            f"{sum(1 for service in services if service._initialized)}"
        )

    async def _close_services(self, services: list[BrowserCaptchaService]) -> None:
        await asyncio.gather(*(service.close() for service in services), return_exceptions=True)

    async def _replace_accounts(self, count: int) -> None:
        async with self.db._connect(write=True) as connection:
            await connection.execute("DELETE FROM tokens")
            await connection.executemany(
                """
                INSERT INTO tokens (
                    st, email, is_active, auto_refresh_enabled,
                    protocol_mode, auth_state, account_profile_key
                ) VALUES (?, ?, 1, 1, 'protocol', 'ok', ?)
                """,
                [
                    (
                        f"synthetic-st-{index}",
                        f"fixture-{index}@example.invalid",
                        f"{index:032x}"[-32:],
                    )
                    for index in range(count)
                ],
            )
            await connection.commit()

    async def test_recovery_candidate_scan_handles_zero_one_two_hundred_and_five_hundred(self):
        for count in (0, 1, 200, 500):
            with self.subTest(count=count):
                await self._replace_accounts(count)
                candidates = await self.db.get_auth_recovery_candidates(
                    datetime.now(timezone.utc)
                )
                self.assertEqual(count, len(candidates))

    async def test_two_hundred_persistent_profiles_do_not_start_browsers_at_rest(self):
        store = AccountProfileStore(Path(self._temp_dir.name) / "profiles")
        with patch.object(
            BrowserCaptchaService,
            "initialize",
            new=AsyncMock(),
        ) as initialize:
            for _ in range(200):
                key = store.create_key()
                store.resolve(key, create=True)
            await asyncio.sleep(0)

        initialize.assert_not_awaited()

    async def test_token_manager_construction_with_profiled_accounts_starts_no_browser(self):
        await self._replace_accounts(200)
        manager = TokenManager(self.db, object())
        self.assertIsNotNone(manager)
        self.assertIsNone(manager._protocol_refresher_task)

    def test_existing_browser_limit_contract_remains_bounded(self):
        self.assertEqual(1, resolve_effective_browser_count(0))
        self.assertEqual(5, resolve_effective_browser_count(5))
        self.assertEqual(10, resolve_effective_browser_count(10))
        self.assertEqual(10, resolve_effective_browser_count(50))

    async def test_live_browser_process_limit_blocks_eleventh_until_one_closes(self):
        services = [self._make_browser_service(str(index)) for index in range(11)]
        eleventh_entered = asyncio.Event()
        release_eleventh = asyncio.Event()
        start_count = 0

        async def start_browser(**_kwargs):
            nonlocal start_count
            start_count += 1
            if start_count == 11:
                eleventh_entered.set()
                await release_eleventh.wait()
            return self._fake_browser()

        tasks = []
        try:
            with self._synthetic_nodriver(start_browser):
                tasks = [asyncio.create_task(service.initialize()) for service in services]
                await self._wait_for_initialized_count(services, 10)
                await asyncio.sleep(0.2)
                self.assertFalse(eleventh_entered.is_set())
                self.assertFalse(tasks[10].done())

                await services[0].close()
                await asyncio.wait_for(eleventh_entered.wait(), timeout=1.0)
                release_eleventh.set()
                await asyncio.wait_for(tasks[10], timeout=1.0)
        finally:
            release_eleventh.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._close_services(services)

    async def test_start_failure_releases_process_lease_before_replacement_launch(self):
        baseline = [self._make_browser_service(f"failure-base-{index}") for index in range(9)]
        failing = self._make_browser_service("failure")
        replacements = [
            self._make_browser_service("failure-replacement-1"),
            self._make_browser_service("failure-replacement-2"),
        ]
        second_replacement_entered = asyncio.Event()
        release_second_replacement = asyncio.Event()
        start_count = 0

        async def start_browser(**_kwargs):
            nonlocal start_count
            start_count += 1
            if start_count == 10:
                raise RuntimeError("synthetic-start-failure")
            if start_count == 12:
                second_replacement_entered.set()
                await release_second_replacement.wait()
            return self._fake_browser()

        replacement_tasks = []
        try:
            with self._synthetic_nodriver(start_browser):
                await asyncio.gather(*(service.initialize() for service in baseline))
                with self.assertRaises(RuntimeError):
                    await failing.initialize()

                replacement_tasks = [
                    asyncio.create_task(service.initialize()) for service in replacements
                ]
                await self._wait_for_initialized_count(replacements, 1)
                await asyncio.sleep(0.2)
                self.assertFalse(second_replacement_entered.is_set())

                await baseline[0].close()
                await asyncio.wait_for(second_replacement_entered.wait(), timeout=1.0)
                release_second_replacement.set()
                await asyncio.gather(*replacement_tasks)
        finally:
            release_second_replacement.set()
            if replacement_tasks:
                await asyncio.gather(*replacement_tasks, return_exceptions=True)
            await self._close_services([*baseline, failing, *replacements])

    async def test_start_timeout_releases_process_lease_before_replacement_launch(self):
        baseline = [self._make_browser_service(f"timeout-base-{index}") for index in range(9)]
        timing_out = self._make_browser_service("timeout")
        replacements = [
            self._make_browser_service("timeout-replacement-1"),
            self._make_browser_service("timeout-replacement-2"),
        ]
        second_replacement_entered = asyncio.Event()
        release_second_replacement = asyncio.Event()
        never_finish = asyncio.Event()
        start_count = 0
        original_run_with_timeout = timing_out._run_with_timeout

        async def short_timeout(awaitable, timeout_seconds, label):
            if label.startswith("nodriver.start"):
                try:
                    return await asyncio.wait_for(awaitable, timeout=0.02)
                except asyncio.TimeoutError as exc:
                    raise TimeoutError("synthetic-start-timeout") from exc
            return await original_run_with_timeout(awaitable, timeout_seconds, label)

        timing_out._run_with_timeout = short_timeout

        async def start_browser(**_kwargs):
            nonlocal start_count
            start_count += 1
            if start_count == 10:
                await never_finish.wait()
            if start_count == 12:
                second_replacement_entered.set()
                await release_second_replacement.wait()
            return self._fake_browser()

        replacement_tasks = []
        try:
            with self._synthetic_nodriver(start_browser):
                await asyncio.gather(*(service.initialize() for service in baseline))
                with self.assertRaises(TimeoutError):
                    await timing_out.initialize()

                replacement_tasks = [
                    asyncio.create_task(service.initialize()) for service in replacements
                ]
                await self._wait_for_initialized_count(replacements, 1)
                await asyncio.sleep(0.2)
                self.assertFalse(second_replacement_entered.is_set())

                await baseline[0].close()
                await asyncio.wait_for(second_replacement_entered.wait(), timeout=1.0)
                release_second_replacement.set()
                await asyncio.gather(*replacement_tasks)
        finally:
            never_finish.set()
            release_second_replacement.set()
            if replacement_tasks:
                await asyncio.gather(*replacement_tasks, return_exceptions=True)
            await self._close_services([*baseline, timing_out, *replacements])

    async def test_cancelled_start_releases_process_lease_before_replacement_launch(self):
        baseline = [self._make_browser_service(f"cancel-base-{index}") for index in range(9)]
        cancelled = self._make_browser_service("cancelled")
        replacements = [
            self._make_browser_service("cancel-replacement-1"),
            self._make_browser_service("cancel-replacement-2"),
        ]
        cancelled_start_entered = asyncio.Event()
        second_replacement_entered = asyncio.Event()
        release_cancelled_start = asyncio.Event()
        release_second_replacement = asyncio.Event()
        start_count = 0

        async def start_browser(**_kwargs):
            nonlocal start_count
            start_count += 1
            if start_count == 10:
                cancelled_start_entered.set()
                await release_cancelled_start.wait()
            if start_count == 12:
                second_replacement_entered.set()
                await release_second_replacement.wait()
            return self._fake_browser()

        cancelled_task = None
        replacement_tasks = []
        try:
            with self._synthetic_nodriver(start_browser):
                await asyncio.gather(*(service.initialize() for service in baseline))
                cancelled_task = asyncio.create_task(cancelled.initialize())
                await asyncio.wait_for(cancelled_start_entered.wait(), timeout=1.0)
                cancelled_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled_task

                replacement_tasks = [
                    asyncio.create_task(service.initialize()) for service in replacements
                ]
                await self._wait_for_initialized_count(replacements, 1)
                await asyncio.sleep(0.2)
                self.assertFalse(second_replacement_entered.is_set())

                await baseline[0].close()
                await asyncio.wait_for(second_replacement_entered.wait(), timeout=1.0)
                release_second_replacement.set()
                await asyncio.gather(*replacement_tasks)
        finally:
            release_cancelled_start.set()
            release_second_replacement.set()
            if cancelled_task is not None and not cancelled_task.done():
                cancelled_task.cancel()
            if replacement_tasks:
                await asyncio.gather(*replacement_tasks, return_exceptions=True)
            await self._close_services([*baseline, cancelled, *replacements])

    async def test_close_exception_still_releases_process_lease(self):
        baseline = [self._make_browser_service(f"close-base-{index}") for index in range(10)]
        replacements = [
            self._make_browser_service("close-replacement-1"),
            self._make_browser_service("close-replacement-2"),
        ]
        second_replacement_entered = asyncio.Event()
        release_second_replacement = asyncio.Event()
        start_count = 0

        async def start_browser(**_kwargs):
            nonlocal start_count
            start_count += 1
            if start_count == 12:
                second_replacement_entered.set()
                await release_second_replacement.wait()
            return self._fake_browser()

        replacement_tasks = []
        try:
            with self._synthetic_nodriver(start_browser):
                await asyncio.gather(*(service.initialize() for service in baseline))
                baseline[0]._cleanup_runtime_profile_dirs_after_shutdown = AsyncMock(
                    side_effect=RuntimeError("synthetic-close-failure")
                )
                await baseline[0].close()

                replacement_tasks = [
                    asyncio.create_task(service.initialize()) for service in replacements
                ]
                await self._wait_for_initialized_count(replacements, 1)
                await asyncio.sleep(0.2)
                self.assertFalse(second_replacement_entered.is_set())

                await baseline[1].close()
                await asyncio.wait_for(second_replacement_entered.wait(), timeout=1.0)
                release_second_replacement.set()
                await asyncio.gather(*replacement_tasks)
        finally:
            release_second_replacement.set()
            if replacement_tasks:
                await asyncio.gather(*replacement_tasks, return_exceptions=True)
            await self._close_services([*baseline, *replacements])

    async def test_idle_runtime_reaper_releases_stale_lease_after_browser_process_dies(self):
        worker = self._make_browser_service("stale-process")

        async def start_browser(**_kwargs):
            return self._fake_browser()

        try:
            with self._synthetic_nodriver(start_browser):
                await worker.initialize()

            self.assertTrue(worker._browser_process_lease_held)
            worker.browser.stopped = True

            did_shutdown = await worker.shutdown_idle_runtime_if_needed(
                idle_ttl_seconds=60,
                reason="synthetic_stale_process",
            )

            self.assertTrue(did_shutdown)
            self.assertFalse(worker._browser_process_lease_held)
        finally:
            await worker.close()

    async def test_personal_pool_and_reauth_recovery_share_same_live_process_limit(self):
        pool = _PersonalBrowserPoolService(None)
        recovery_services = [self._make_browser_service(f"recovery-{index}") for index in range(5)]
        eleventh_entered = asyncio.Event()
        release_eleventh = asyncio.Event()
        start_count = 0
        tasks = []

        async def start_browser(**_kwargs):
            nonlocal start_count
            start_count += 1
            if start_count == 11:
                eleventh_entered.set()
                await release_eleventh.wait()
            return self._fake_browser()

        try:
            with patch.object(
                BrowserCaptchaService,
                "_resolve_configured_browser_count",
                return_value=6,
            ), patch.object(
                BrowserCaptchaService,
                "_resolve_user_data_dir",
                return_value=None,
            ), patch.object(
                BrowserCaptchaService,
                "_idle_tab_reaper_loop",
                new=AsyncMock(),
            ):
                await pool._ensure_workers()
            pool_workers = [self._prepare_browser_service(worker) for worker in pool._workers]
            all_services = [*pool_workers, *recovery_services]
            self.assertEqual(11, len(all_services))

            with self._synthetic_nodriver(start_browser):
                tasks = [asyncio.create_task(service.initialize()) for service in all_services]
                await self._wait_for_initialized_count(all_services, 10)
                await asyncio.sleep(0.2)
                self.assertFalse(eleventh_entered.is_set())

                await pool_workers[0].close()
                await asyncio.wait_for(eleventh_entered.wait(), timeout=1.0)
                release_eleventh.set()
                await asyncio.wait_for(tasks[10], timeout=1.0)
        finally:
            release_eleventh.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await pool.close()
            await self._close_services(recovery_services)


if __name__ == "__main__":
    unittest.main()
