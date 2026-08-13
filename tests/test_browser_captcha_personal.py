import itertools
import json
import types
import unittest
from unittest.mock import AsyncMock

from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    ResidentTabInfo,
    _PersonalBrowserPoolService,
    _build_personal_browser_args,
    _patch_nodriver_connection_instance,
)


class _FakeTab:
    def __init__(self, result):
        self._result = result

    async def evaluate(self, expression, await_promise=False, return_by_value=False):
        return self._result


class _ClosableFakeTab:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True

    async def sleep(self, _seconds):
        return None


class _FakeWebSocket:
    def __init__(self, owner):
        self.owner = owner
        self.close_code = None
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        payload = json.loads(message)
        transaction = self.owner.mapper[payload["id"]]
        transaction(result={"ok": True})


class _ConnectionWithoutClosed:
    def __init__(self):
        self.mapper = {}
        self.handlers = {}
        self.websocket = None
        self.connect_count = 0
        self.register_count = 0
        self.__count__ = itertools.count(0)

    async def send(self, _cdp_obj, _is_update=False):
        raise AssertionError("original send should be patched")

    async def connect(self):
        self.connect_count += 1
        self.websocket = _FakeWebSocket(self)

    async def _register_handlers(self):
        self.register_count += 1


def _fake_cdp_command():
    result = yield {"method": "Runtime.evaluate", "params": {}}
    return result


class BrowserCaptchaPersonalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()

    def test_daily_personal_runtime_is_background_and_context_isolated_by_default(self):
        service = BrowserCaptchaService()

        self.assertTrue(service.headless)

    def test_visible_account_onboarding_uses_temporary_regular_profile_not_incognito(self):
        service = BrowserCaptchaService(force_headed=True)
        args = _build_personal_browser_args(
            headless=service.headless,
            incognito_enabled=service.runtime_incognito_enabled,
        )

        self.assertFalse(service.headless)
        self.assertNotIn("--incognito", args)
        self.assertNotIn("--bwsi", args)

    @staticmethod
    def _make_remote_object_result(token: str):
        return types.SimpleNamespace(
            type_="object",
            value=None,
            deep_serialized_value=types.SimpleNamespace(
                type_="object",
                value=[
                    ["ok", {"type": "boolean", "value": True}],
                    ["token", {"type": "string", "value": token}],
                ],
            ),
        )

    async def test_tab_evaluate_normalizes_deep_serialized_remote_object(self):
        tab = _FakeTab(self._make_remote_object_result("token-123"))

        result = await self.service._tab_evaluate(
            tab,
            "ignored",
            label="unit_test_tab_evaluate",
            await_promise=True,
            return_by_value=True,
        )

        self.assertEqual(result, {"ok": True, "token": "token-123"})

    async def test_execute_recaptcha_on_tab_accepts_remote_object_success_result(self):
        tab = _FakeTab(self._make_remote_object_result("token-xyz"))

        token = await self.service._execute_recaptcha_on_tab(tab, action="IMAGE_GENERATION")

        self.assertEqual(token, "token-xyz")

    async def test_create_resident_tab_returns_none_when_browser_missing(self):
        self.service.browser = None

        resident_info = await self.service._create_resident_tab("slot-1", project_id="project-1")

        self.assertIsNone(resident_info)

    async def test_close_clears_resident_tabs_when_warmup_task_attr_missing(self):
        tab = _ClosableFakeTab()
        self.service._resident_tabs["slot-1"] = ResidentTabInfo(tab=tab, slot_id="slot-1")
        if hasattr(self.service, "_resident_warmup_task"):
            delattr(self.service, "_resident_warmup_task")

        await self.service.close()

        self.assertEqual(self.service._resident_tabs, {})
        self.assertTrue(tab.closed)

    async def test_create_resident_tab_cleans_tab_when_initialization_fails(self):
        tab = _ClosableFakeTab()
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._create_isolated_context_tab = AsyncMock(return_value=(tab, "context-1"))
        self.service._tab_evaluate = AsyncMock(return_value="complete")
        self.service._apply_token_cookie_binding = AsyncMock(side_effect=RuntimeError("cookie failed"))
        self.service._dispose_browser_context_quietly = AsyncMock()
        self.service._close_tab_quietly = AsyncMock()

        resident_info = await self.service._create_resident_tab("slot-1", project_id="project-1")

        self.assertIsNone(resident_info)
        self.service._dispose_browser_context_quietly.assert_awaited_once_with("context-1")
        self.service._close_tab_quietly.assert_awaited_once_with(tab)

    async def test_restart_browser_for_project_reuses_recent_healthy_runtime(self):
        resident_info = ResidentTabInfo(tab=object(), slot_id="slot-1", project_id="project-1")
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._initialized = True
        self.service._mark_runtime_restart()
        self.service._probe_browser_runtime = AsyncMock(return_value=True)
        self.service._ensure_resident_tab = AsyncMock(return_value=("slot-1", resident_info))
        self.service._restart_browser_for_project_unlocked = AsyncMock(return_value=True)

        result = await self.service._restart_browser_for_project("project-1")

        self.assertTrue(result)
        self.service._restart_browser_for_project_unlocked.assert_not_awaited()
        self.service._ensure_resident_tab.assert_awaited_once()

    async def test_wait_for_recaptcha_raises_on_runtime_disconnect(self):
        tab = _ClosableFakeTab()
        runtime_error = ConnectionRefusedError(1225, "远程计算机拒绝网络连接。")
        self.service._inject_recaptcha_bootstrap_script = AsyncMock(return_value="remote")
        self.service._tab_evaluate = AsyncMock(side_effect=runtime_error)

        with self.assertRaises(ConnectionRefusedError):
            await self.service._wait_for_recaptcha(tab)

        self.assertFalse(self.service._last_health_probe_ok)
        self.assertEqual(self.service._tab_evaluate.await_count, 1)

    async def test_force_fresh_flow_error_defers_sync_browser_restart_until_drain(self):
        tab = _ClosableFakeTab()
        resident_info = ResidentTabInfo(
            tab=tab,
            slot_id="slot-1",
            project_id="project-1",
            token_id=1,
        )
        resident_info.recaptcha_ready = True
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._initialized = True
        self.service._resident_tabs["slot-1"] = resident_info
        self.service._project_resident_affinity["project-1"] = "slot-1"
        self.service._token_resident_affinity["1"] = "slot-1"
        self.service._maybe_execute_pending_fresh_profile_restart = AsyncMock(return_value=False)
        self.service._restart_browser_for_project = AsyncMock(return_value=True)

        await self.service.report_flow_error(
            "project-1",
            "reCAPTCHA 验证失败",
            error_message="Flow API request failed: PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed",
            token_id=1,
            slot_id="slot-1",
        )

        self.assertIn("slot-1", self.service._resident_unavailable_slots)
        self.assertTrue(self.service._fresh_profile_restart_pending)
        self.assertTrue(self.service._fresh_profile_restart_force_pending)
        self.service._restart_browser_for_project.assert_not_awaited()
        self.service._maybe_execute_pending_fresh_profile_restart.assert_awaited_once()

    async def test_pending_fresh_restart_task_is_preserved_during_runtime_shutdown(self):
        async def runner():
            self.service._fresh_profile_restart_task = asyncio.current_task()
            await self.service._cancel_background_runtime_tasks(reason="unit_test")
            self.assertIs(self.service._fresh_profile_restart_task, asyncio.current_task())

        import asyncio

        task = asyncio.create_task(runner())
        await task

    async def test_get_token_waits_for_pending_fresh_restart_before_resident_pick(self):
        import asyncio

        events = []
        tab = _ClosableFakeTab()
        resident_info = ResidentTabInfo(
            tab=tab,
            slot_id="slot-1",
            project_id="project-1",
            token_id=1,
        )
        resident_info.recaptcha_ready = True
        self.service._fresh_profile_restart_every_n_solves = 5
        self.service._fresh_profile_restart_pending = True
        self.service._fresh_profile_restart_pending_reason = "unit:5/5"
        self.service._has_active_browser_work = AsyncMock(return_value=False)

        async def restart_unlocked(project_id, token_id=None, *, fresh_profile=False):
            events.append("fresh_restart")
            self.assertEqual(project_id, "project-1")
            self.assertTrue(fresh_profile)
            self.service._reset_browser_rotation_budget()
            return True

        async def initialize():
            events.append("initialize")

        async def ensure_resident(*args, **kwargs):
            events.append("ensure_resident")
            return "slot-1", resident_info

        async def solve_resident(*args, **kwargs):
            events.append("solve_resident")
            return "token-1"

        self.service._restart_browser_for_project_unlocked = AsyncMock(side_effect=restart_unlocked)
        self.service.initialize = AsyncMock(side_effect=initialize)
        self.service._ensure_resident_tab = AsyncMock(side_effect=ensure_resident)
        self.service._ensure_resident_token_binding = AsyncMock(return_value=True)
        self.service._solve_with_resident_tab = AsyncMock(side_effect=solve_resident)

        token, slot_id = await self.service._get_token_direct(
            "project-1",
            token_id=1,
            return_slot_id=True,
        )

        self.assertEqual((token, slot_id), ("token-1", "slot-1"))
        self.assertEqual(events, ["fresh_restart", "initialize", "ensure_resident", "solve_resident"])
        self.assertFalse(self.service._fresh_profile_restart_pending)
        self.assertIsNone(self.service._fresh_profile_restart_task)

    async def test_wait_for_pending_fresh_restart_awaits_existing_task(self):
        import asyncio

        events = []
        self.service._fresh_profile_restart_pending = True

        async def restart_task():
            events.append("restart_start")
            await asyncio.sleep(0.01)
            self.service._fresh_profile_restart_pending = False
            events.append("restart_done")
            return True

        task = asyncio.create_task(restart_task())
        self.service._fresh_profile_restart_task = task

        result = await self.service._wait_for_pending_fresh_profile_restart_before_solve(
            "project-1",
            token_id=1,
            source="unit_test",
        )

        self.assertTrue(result)
        self.assertEqual(events, ["restart_start", "restart_done"])
        self.assertTrue(task.done())

    async def test_runtime_surface_profile_contains_extended_browser_environment(self):
        profile = self.service._get_runtime_surface_profile()

        self.assertIn("webgpu", profile)
        self.assertIn("mediaQueries", profile)
        self.assertIn("storage", profile)
        self.assertIn("behavior", profile)
        self.assertIn("visualViewport", profile["window"])
        self.assertIn("supportedExtensions", profile["graphics"])
        self.assertIn("WEBGL_debug_renderer_info", profile["graphics"]["supportedExtensions"])

        source = self.service._build_tab_fingerprint_spoof_source(types.SimpleNamespace(target_id="unit-tab"))
        for marker in (
            "ensureWebGpuEnvironment",
            "ensureMatchMediaEnvironment",
            "ensureVisualViewportEnvironment",
            "navigator.storage",
            "getSupportedConstraints",
            "userActivation",
        ):
            self.assertIn(marker, source)

    async def test_pool_tab_limits_enforce_ten_process_cap_and_preserve_fifty_tabs(self):
        pool = _PersonalBrowserPoolService()

        self.assertEqual(pool._build_worker_tab_limits(5, 10), [5] * 10)

        capped_limits = pool._build_worker_tab_limits(5, 20)
        self.assertEqual(len(capped_limits), 10)
        self.assertEqual(sum(capped_limits), 50)
        self.assertLessEqual(max(capped_limits), 5)

        warmup_limits = pool._build_worker_tab_limits(
            5,
            10,
            total_limit=5,
            allow_zero=True,
        )
        self.assertEqual(len(warmup_limits), 10)
        self.assertEqual(sum(warmup_limits), 5)
        self.assertEqual(sum(1 for item in warmup_limits if item > 0), 5)

    async def test_pool_dispatch_prefers_cold_idle_worker_over_busy_live_worker(self):
        pool = _PersonalBrowserPoolService()
        live_worker = BrowserCaptchaService(browser_instance_id=1, max_resident_tabs_override=5)
        cold_worker = BrowserCaptchaService(browser_instance_id=2, max_resident_tabs_override=5)
        live_worker._initialized = True
        live_worker.browser = types.SimpleNamespace(stopped=False)
        pool._workers = [live_worker, cold_worker]
        pool._worker_dispatch_reservations = {0: 1}

        self.assertLess(
            pool._worker_dispatch_score(1, cold_worker),
            pool._worker_dispatch_score(0, live_worker),
        )

    async def test_nodriver_send_patch_handles_connection_without_closed_attr(self):
        connection = _ConnectionWithoutClosed()

        _patch_nodriver_connection_instance(connection)
        result = await connection.send(_fake_cdp_command())

        self.assertEqual(result, {"ok": True})
        self.assertEqual(connection.connect_count, 1)
        self.assertEqual(connection.register_count, 1)
        self.assertTrue(getattr(connection, "_flow2api_send_patched", False))

    async def test_selected_accounts_are_injected_only_into_their_own_contexts(self):
        cookies_by_token = {
            1: types.SimpleNamespace(google_cookies="SID=opaque-account-a"),
            2: types.SimpleNamespace(google_cookies="SID=opaque-account-b"),
        }
        self.service.db = types.SimpleNamespace(
            get_token=AsyncMock(side_effect=lambda token_id: cookies_by_token[token_id])
        )
        self.service._set_browser_cookie_targets = AsyncMock(return_value=1)
        self.service._tab_reload = AsyncMock()
        resident_a = ResidentTabInfo(
            tab=object(),
            slot_id="slot-a",
            browser_context_id="context-a",
        )
        resident_b = ResidentTabInfo(
            tab=object(),
            slot_id="slot-b",
            browser_context_id="context-b",
        )

        first_ok = await self.service._apply_token_cookie_binding(
            resident_a,
            1,
            label="account-a",
        )
        second_ok = await self.service._apply_token_cookie_binding(
            resident_b,
            2,
            label="account-b",
        )

        calls = self.service._set_browser_cookie_targets.await_args_list
        first_values = {item["value"] for item in calls[0].args[0]}
        second_values = {item["value"] for item in calls[1].args[0]}
        self.assertTrue(first_ok)
        self.assertTrue(second_ok)
        self.assertEqual({"opaque-account-a"}, first_values)
        self.assertEqual({"opaque-account-b"}, second_values)
        self.assertEqual("context-a", calls[0].kwargs["browser_context_id"])
        self.assertEqual("context-b", calls[1].kwargs["browser_context_id"])

    async def test_same_token_reuses_resident_binding_without_reinjection(self):
        self.service.db = types.SimpleNamespace(
            get_token=AsyncMock(
                return_value=types.SimpleNamespace(
                    google_cookies="SID=opaque-account-a",
                )
            )
        )
        self.service._set_browser_cookie_targets = AsyncMock(return_value=1)
        self.service._tab_reload = AsyncMock()
        resident = ResidentTabInfo(
            tab=object(),
            slot_id="slot-a",
            browser_context_id="context-a",
        )

        first_ok = await self.service._apply_token_cookie_binding(
            resident,
            1,
            label="first",
        )
        self.service._set_browser_cookie_targets.reset_mock()
        self.service._tab_reload.reset_mock()
        second_ok = await self.service._apply_token_cookie_binding(
            resident,
            1,
            label="second",
        )

        self.assertTrue(first_ok)
        self.assertTrue(second_ok)
        self.assertEqual(1, resident.token_id)
        self.assertEqual("context-a", resident.browser_context_id)
        self.service._set_browser_cookie_targets.assert_not_awaited()
        self.service._tab_reload.assert_not_awaited()

    async def test_failed_context_replacement_does_not_fall_back_to_different_token(self):
        self.service._max_resident_tabs = 2
        old_resident = ResidentTabInfo(
            tab=object(),
            slot_id="slot-old",
            project_id="project-a",
            token_id=1,
            browser_context_id="context-a",
        )
        old_resident.recaptcha_ready = True
        self.service._resident_tabs["slot-old"] = old_resident
        self.service._token_resident_affinity["1"] = "slot-old"
        self.service._project_resident_affinity["project-a"] = "slot-old"
        self.service._create_resident_tab = AsyncMock(return_value=None)

        slot_id, selected = await self.service._ensure_resident_tab(
            "project-b",
            token_id=2,
            return_slot_key=True,
        )

        self.assertIsNone(slot_id)
        self.assertIsNone(selected)
        self.assertEqual({"slot-old": old_resident}, self.service._resident_tabs)
        self.assertEqual("context-a", old_resident.browser_context_id)
        self.assertEqual(1, old_resident.token_id)

    async def test_different_token_replaces_capacity_context_and_clears_old_affinity(self):
        self.service._max_resident_tabs = 1
        old_resident = ResidentTabInfo(
            tab=object(),
            slot_id="slot-old",
            project_id="project-a",
            token_id=1,
            browser_context_id="context-a",
        )
        old_resident.recaptcha_ready = True
        new_resident = ResidentTabInfo(
            tab=object(),
            slot_id="slot-1",
            project_id="project-b",
            token_id=2,
            browser_context_id="context-b",
        )
        new_resident.recaptcha_ready = True
        self.service._resident_tabs["slot-old"] = old_resident
        self.service._token_resident_affinity["1"] = "slot-old"
        self.service._project_resident_affinity["project-a"] = "slot-old"
        self.service._dispose_browser_context_quietly = AsyncMock()
        self.service._close_tab_quietly = AsyncMock()
        self.service._create_resident_tab = AsyncMock(return_value=new_resident)

        slot_id, selected = await self.service._ensure_resident_tab(
            "project-b",
            token_id=2,
            return_slot_key=True,
        )

        self.assertEqual("slot-1", slot_id)
        self.assertIs(new_resident, selected)
        self.assertEqual({"slot-1": new_resident}, self.service._resident_tabs)
        self.assertNotIn("1", self.service._token_resident_affinity)
        self.assertEqual("slot-1", self.service._token_resident_affinity["2"])
        self.service._dispose_browser_context_quietly.assert_awaited_once_with("context-a")
        self.service._close_tab_quietly.assert_awaited_once_with(old_resident.tab)


if __name__ == "__main__":
    unittest.main()
