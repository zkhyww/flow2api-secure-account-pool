import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin
from src.services.account_onboarding import AccountOnboardingService
from src.services.account_profile_store import AccountProfileStore


PUBLIC_FIELDS = {
    "session_id",
    "stage",
    "status",
    "started_at",
    "expires_at",
    "account_count_before",
    "account_count_after",
    "error_class",
}


class _BrowserHandle:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


async def _wait_for_terminal(service, session_id):
    for _ in range(100):
        state = await service.status(session_id)
        if state.status in service.TERMINAL_STATUSES:
            return state
        await asyncio.sleep(0.005)
    raise AssertionError("onboarding session did not become terminal")


class AccountOnboardingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_start_reuses_one_session_and_launches_one_browser(self):
        launches = []
        release = asyncio.Event()

        async def launch(_session_id):
            launches.append(True)
            await release.wait()
            return "success"

        service = AccountOnboardingService(
            account_counter=lambda: asyncio.sleep(0, result=2),
            browser_launcher=launch,
            ttl_seconds=30,
            poll_interval_seconds=0.01,
        )
        first = await service.start()
        second = await service.start()
        await asyncio.sleep(0)

        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(1, len(launches))
        release.set()
        await _wait_for_terminal(service, first.session_id)

    async def test_native_import_result_finishes_successfully_with_updated_count(self):
        count = 2

        async def account_counter():
            return count

        async def launch(_session_id):
            nonlocal count
            count = 3
            return "success"

        service = AccountOnboardingService(
            account_counter=account_counter,
            browser_launcher=launch,
            ttl_seconds=30,
            poll_interval_seconds=0.01,
        )
        state = await service.start()
        current = await _wait_for_terminal(service, state.session_id)

        self.assertEqual("success", current.status)
        self.assertEqual("success", current.stage)
        self.assertEqual(2, current.account_count_before)
        self.assertEqual(3, current.account_count_after)

    async def test_timeout_is_terminal_and_launcher_cleanup_runs(self):
        handle = _BrowserHandle()
        never = asyncio.Event()

        async def launch(_session_id):
            try:
                await never.wait()
            finally:
                await handle.close()

        service = AccountOnboardingService(
            account_counter=lambda: asyncio.sleep(0, result=2),
            browser_launcher=launch,
            ttl_seconds=0.03,
            poll_interval_seconds=0.005,
        )
        state = await service.start()
        await asyncio.sleep(0.06)
        current = await service.status(state.session_id)

        self.assertEqual("timeout", current.status)
        self.assertEqual("timeout", current.error_class)
        self.assertEqual(1, handle.closed)

    async def test_browser_launch_itself_is_bounded_by_remaining_ttl(self):
        never = asyncio.Event()

        async def stuck_launch(_session_id):
            await never.wait()

        service = AccountOnboardingService(
            account_counter=lambda: asyncio.sleep(0, result=2),
            browser_launcher=stuck_launch,
            ttl_seconds=0.03,
            poll_interval_seconds=0.005,
        )
        first = await service.start()
        await asyncio.sleep(0.06)
        current = await service.status(first.session_id)

        self.assertEqual("timeout", current.status)
        self.assertEqual("timeout", current.error_class)
        second = await service.start()
        self.assertNotEqual(first.session_id, second.session_id)
        await service.finish(second.session_id, "failed")

    async def test_public_state_is_strictly_allowlisted(self):
        never = asyncio.Event()
        service = AccountOnboardingService(
            account_counter=lambda: asyncio.sleep(0, result=0),
            browser_launcher=lambda _session_id: never.wait(),
            ttl_seconds=30,
        )
        state = await service.start()
        self.assertEqual(PUBLIC_FIELDS, set(state.to_public_dict()))
        await service.finish(state.session_id, "failed")

    async def test_unexpected_onboarding_error_uses_stable_public_error_class(self):
        sentinel = "SENSITIVE_ONBOARDING_URL_AND_RESPONSE_BODY"

        async def launch(_session_id):
            raise RuntimeError(sentinel)

        service = AccountOnboardingService(
            account_counter=lambda: asyncio.sleep(0, result=0),
            browser_launcher=launch,
            ttl_seconds=30,
        )
        with patch(
            "src.services.account_onboarding.debug_logger.log_warning"
        ) as warning_log:
            state = await service.start()
            current = await _wait_for_terminal(service, state.session_id)

        self.assertEqual("failed", current.status)
        self.assertEqual("failed", current.error_class)
        self.assertNotIn(sentinel, str(warning_log.call_args_list))
        self.assertNotIn("RuntimeError", str(warning_log.call_args_list))


class AccountOnboardingApiAndPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_service = admin.account_onboarding_service
        self.never = asyncio.Event()
        self.service = AccountOnboardingService(
            account_counter=lambda: asyncio.sleep(0, result=1),
            browser_launcher=lambda _session_id: self.never.wait(),
            ttl_seconds=30,
        )
        admin.account_onboarding_service = self.service
        admin.active_admin_tokens.add("admin-fixture")
        app = FastAPI()
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        for session_id in list(self.service._states):
            await self.service.finish(session_id, "failed")
        admin.active_admin_tokens.discard("admin-fixture")
        admin.account_onboarding_service = self.original_service
        await self.client.aclose()

    async def test_start_and_status_require_admin_and_return_only_public_fields(self):
        unauthorized = await self.client.post("/api/admin/account-onboarding")
        self.assertEqual(401, unauthorized.status_code)

        headers = {"Authorization": "Bearer admin-fixture"}
        started = await self.client.post("/api/admin/account-onboarding", headers=headers)
        self.assertEqual(200, started.status_code)
        self.assertEqual(PUBLIC_FIELDS, set(started.json()))

        status = await self.client.get(
            f"/api/admin/account-onboarding/{started.json()['session_id']}",
            headers=headers,
        )
        self.assertEqual(200, status.status_code)
        self.assertEqual(PUBLIC_FIELDS, set(status.json()))

    def test_manage_page_wires_idempotent_button_and_polling_state(self):
        html = Path("static/manage.html").read_text(encoding="utf-8")
        self.assertIn('id="browserAccountOnboardingBtn"', html)
        self.assertIn("startBrowserAccountOnboarding", html)
        self.assertIn("pollBrowserAccountOnboarding", html)
        self.assertRegex(html, r"browserAccountOnboardingBtn[^\n]{0,500}disabled")

    def test_manage_page_keeps_google_onboarding_primary_and_manual_token_advanced_at_narrow_widths(self):
        html = Path("static/manage.html").read_text(encoding="utf-8")
        toolbar = html[html.index("<!-- Token 列表 -->"):html.index('id="tokenLoadState"')]

        self.assertIn("flex-wrap", toolbar)
        self.assertIn('id="browserAccountOnboardingBtn"', toolbar)
        self.assertIn("添加新 Google 账号", toolbar)
        self.assertLess(
            toolbar.index('id="browserAccountOnboardingBtn"'),
            toolbar.index('onclick="openAddModal()"'),
        )
        self.assertIn("高级：手动添加 Token", toolbar)

    async def _assert_failed_launcher_removes_profile(
        self,
        *,
        capture_error=None,
        persist_error=None,
    ):
        original_token_manager = admin.token_manager
        original_service = admin.account_onboarding_service
        captured = {}

        class FakeBrowser:
            def __init__(self, *_args, **kwargs):
                captured["profile_dir"] = kwargs["persistent_profile_dir"]
                if capture_error is not None:
                    self.capture_account_onboarding_result = AsyncMock(
                        side_effect=capture_error,
                    )
                else:
                    self.capture_account_onboarding_result = AsyncMock(
                        return_value={
                            "st": "opaque-session-native",
                            "google_cookies": "SID=opaque-cookie-native",
                        },
                    )
                self.close = AsyncMock()
                captured["instance"] = self

        admin.token_manager = SimpleNamespace(
            get_all_tokens=AsyncMock(return_value=[]),
        )
        admin.account_onboarding_service = None
        persist = AsyncMock(side_effect=persist_error)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                store = AccountProfileStore(Path(temp_dir))
                with patch(
                    "src.services.browser_captcha_personal.BrowserCaptchaService",
                    new=FakeBrowser,
                ), patch.object(
                    admin,
                    "_get_account_profile_store",
                    return_value=store,
                    create=True,
                ), patch.object(
                    admin,
                    "_persist_native_onboarding_result",
                    new=persist,
                    create=True,
                ):
                    service = await admin._get_account_onboarding_service()
                    state = await service.start()
                    current = await _wait_for_terminal(service, state.session_id)

                self.assertEqual("failed", current.status)
                captured["instance"].close.assert_awaited_once()
                self.assertFalse(captured["profile_dir"].exists())
        finally:
            admin.token_manager = original_token_manager
            admin.account_onboarding_service = original_service

    async def test_launcher_capture_failure_removes_unpersisted_profile(self):
        await self._assert_failed_launcher_removes_profile(
            capture_error=RuntimeError("synthetic capture failure"),
        )

    async def test_launcher_persist_failure_removes_unpersisted_profile(self):
        await self._assert_failed_launcher_removes_profile(
            persist_error=RuntimeError("synthetic persist failure"),
        )

    async def test_real_admin_launcher_allocates_persistent_profile_and_closes_browser(self):
        original_token_manager = admin.token_manager
        original_service = admin.account_onboarding_service
        captured = {}
        private_result = {
            "st": "opaque-session-native",
            "google_cookies": "SID=opaque-cookie-native",
        }

        class FakeBrowser:
            def __init__(self, *_args, **kwargs):
                captured["constructor_kwargs"] = dict(kwargs)
                self.capture_account_onboarding_result = AsyncMock(
                    return_value=private_result,
                )
                self.close = AsyncMock()
                captured["instance"] = self

        admin.token_manager = SimpleNamespace(
            get_all_tokens=AsyncMock(return_value=[]),
        )
        admin.account_onboarding_service = None
        persist = AsyncMock(return_value="success")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                store = AccountProfileStore(Path(temp_dir))
                with patch(
                    "src.services.browser_captcha_personal.BrowserCaptchaService",
                    new=FakeBrowser,
                ), patch.object(
                    admin,
                    "_get_account_profile_store",
                    return_value=store,
                    create=True,
                ), patch.object(
                    admin,
                    "_persist_native_onboarding_result",
                    new=persist,
                    create=True,
                ), patch(
                    "shutil.copytree",
                    side_effect=AssertionError("extension copy is forbidden"),
                ), patch(
                    "src.services.extension_pairing.get_extension_pairing_service",
                    side_effect=AssertionError("pairing service is forbidden"),
                ):
                    service = await admin._get_account_onboarding_service()
                    state = await service.start()
                    current = await _wait_for_terminal(service, state.session_id)

                profile_key = persist.await_args.kwargs["account_profile_key"]
                profile_dir = store.resolve(profile_key)
                self.assertEqual("success", current.status)
                self.assertNotIn(
                    "extension_directory",
                    captured["constructor_kwargs"],
                )
                self.assertTrue(captured["constructor_kwargs"]["force_headed"])
                self.assertEqual(profile_dir, captured["constructor_kwargs"]["persistent_profile_dir"])
                captured["instance"].capture_account_onboarding_result.assert_awaited_once()
                persist.assert_awaited_once_with(
                    private_result,
                    account_profile_key=profile_key,
                )
                captured["instance"].close.assert_awaited_once()
                self.assertTrue(profile_dir.is_dir())
        finally:
            admin.token_manager = original_token_manager
            admin.account_onboarding_service = original_service


if __name__ == "__main__":
    unittest.main()
