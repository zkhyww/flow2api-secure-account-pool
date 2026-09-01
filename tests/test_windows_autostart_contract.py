import importlib
import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.api import admin


MODULE_NAME = "src.services.windows_autostart"
TASK_NAME = "Flow2API-Local-Account-Pool"
REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeTaskBackend:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot or {"exists": False}
        self.inspect_calls = 0
        self.registered_specs = []
        self.unregistered_names = []
        self.fail_inspect = False
        self.fail_register = False
        self.fail_unregister = False

    def inspect(self, task_name):
        self.inspect_calls += 1
        if self.fail_inspect:
            raise RuntimeError("private inspect fixture")
        return dict(self.snapshot)

    def register(self, spec):
        self.registered_specs.append(spec)
        if self.fail_register:
            raise RuntimeError("private register fixture")
        self.snapshot = _exact_snapshot(enabled=True)

    def unregister(self, task_name):
        self.unregistered_names.append(task_name)
        if self.fail_unregister:
            raise RuntimeError("private unregister fixture")
        self.snapshot = {"exists": False}


def _exact_snapshot(*, enabled=True):
    return {
        "exists": True,
        "enabled": enabled,
        "execute": str(REPO_ROOT / "venv" / "Scripts" / "python.exe"),
        "arguments": "main.py",
        "working_directory": str(REPO_ROOT),
        "trigger_type": "logon",
        "trigger_user": "FIXTURE\\Owner",
        "principal_user": "FIXTURE\\Owner",
        "current_user": "FIXTURE\\Owner",
        "start_when_available": True,
        "restart_count": 3,
    }


class WindowsAutostartServiceContractTests(unittest.TestCase):
    def _load_module(self):
        try:
            module_spec = importlib.util.find_spec(MODULE_NAME)
        except ModuleNotFoundError:
            module_spec = None
        self.assertIsNotNone(module_spec, "missing Windows autostart service")
        return importlib.import_module(MODULE_NAME)

    def _manager(self, snapshot=None, *, platform_name="nt"):
        module = self._load_module()
        backend = _FakeTaskBackend(snapshot)
        manager = module.WindowsAutostartManager(
            repo_root=REPO_ROOT,
            platform_name=platform_name,
            backend=backend,
        )
        return module, manager, backend

    def test_non_windows_is_unsupported_without_touching_system_backend(self):
        _, manager, backend = self._manager(platform_name="posix")
        self.assertEqual("unsupported", manager.get_status()["status"])
        self.assertEqual("unsupported", manager.set_enabled(True)["status"])
        self.assertEqual(0, backend.inspect_calls)
        self.assertEqual([], backend.registered_specs)
        self.assertEqual([], backend.unregistered_names)

    def test_windows_status_comes_from_exact_fixed_task_template(self):
        _, manager, _ = self._manager(_exact_snapshot())
        self.assertEqual("enabled", manager.get_status()["status"])

        mismatches = (
            {**_exact_snapshot(), "enabled": False},
            {**_exact_snapshot(), "execute": "C:\\other\\python.exe"},
            {**_exact_snapshot(), "arguments": "other.py"},
            {**_exact_snapshot(), "working_directory": "C:\\other"},
            {**_exact_snapshot(), "trigger_type": "daily"},
            {**_exact_snapshot(), "trigger_user": "FIXTURE\\Other"},
            {**_exact_snapshot(), "start_when_available": False},
            {**_exact_snapshot(), "restart_count": 2},
            {"exists": False},
        )
        for snapshot in mismatches:
            with self.subTest(snapshot=snapshot):
                _, candidate, _ = self._manager(snapshot)
                self.assertEqual("disabled", candidate.get_status()["status"])

    def test_windows_identity_accepts_current_user_qualified_and_task_principal_bare(self):
        snapshot = {
            **_exact_snapshot(),
            "current_user": "DEVBOX\\operator",
            "trigger_user": "DEVBOX\\operator",
            "principal_user": "operator",
        }
        _, manager, _ = self._manager(snapshot)
        self.assertEqual("enabled", manager.get_status()["status"])

    def test_windows_identity_rejects_different_bare_user_and_cross_domain_same_leaf(self):
        mismatches = (
            {
                **_exact_snapshot(),
                "current_user": "DEVBOX\\operator",
                "trigger_user": "DEVBOX\\operator",
                "principal_user": "different-user",
            },
            {
                **_exact_snapshot(),
                "current_user": "DOMAIN1\\same-user",
                "trigger_user": "DOMAIN1\\same-user",
                "principal_user": "DOMAIN2\\same-user",
            },
        )
        for snapshot in mismatches:
            with self.subTest(principal_user=snapshot["principal_user"]):
                _, manager, _ = self._manager(snapshot)
                self.assertEqual("disabled", manager.get_status()["status"])

    def test_query_failure_is_error_without_leaking_backend_text(self):
        _, manager, backend = self._manager()
        backend.fail_inspect = True
        status = manager.get_status()
        self.assertEqual("error", status["status"])
        self.assertIn("读取", status["reason"])
        self.assertNotIn("private inspect fixture", status["reason"])

    def test_enable_uses_only_server_owned_fixed_spec_and_is_idempotent(self):
        module, manager, backend = self._manager({"exists": False})
        first = manager.set_enabled(True)
        second = manager.set_enabled(True)

        self.assertEqual("enabled", first["status"])
        self.assertEqual("enabled", second["status"])
        self.assertEqual(1, len(backend.registered_specs))
        spec = backend.registered_specs[0]
        self.assertEqual(TASK_NAME, spec.task_name)
        self.assertEqual(REPO_ROOT / "venv" / "Scripts" / "python.exe", spec.execute)
        self.assertEqual("main.py", spec.arguments)
        self.assertEqual(REPO_ROOT, spec.working_directory)
        self.assertTrue(spec.start_when_available)
        self.assertEqual(3, spec.restart_count)
        self.assertEqual(TASK_NAME, module.TASK_NAME)

    def test_disable_removes_only_the_fixed_task_and_is_idempotent(self):
        _, manager, backend = self._manager(_exact_snapshot())
        self.assertEqual("disabled", manager.set_enabled(False)["status"])
        self.assertEqual("disabled", manager.set_enabled(False)["status"])
        self.assertEqual([TASK_NAME], backend.unregistered_names)

    def test_failed_mutation_keeps_fresh_real_state_and_bounded_reason(self):
        _, manager, backend = self._manager({"exists": False})
        backend.fail_register = True
        result = manager.set_enabled(True)
        self.assertEqual("disabled", result["status"])
        self.assertIn("启用失败", result["reason"])
        self.assertNotIn("private register fixture", result["reason"])
        self.assertGreaterEqual(backend.inspect_calls, 2)


class _FakeAutostartManager:
    def __init__(self):
        self.enabled = False
        self.calls = []

    def get_status(self):
        return {"status": "enabled" if self.enabled else "disabled", "reason": "fixture"}

    def set_enabled(self, enabled):
        self.calls.append(bool(enabled))
        self.enabled = bool(enabled)
        return self.get_status()


class WindowsAutostartAdminApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake_manager = _FakeAutostartManager()
        app = FastAPI()
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        admin.active_admin_tokens.add("admin-session-fixture")
        self.manager_patch = patch.object(
            admin,
            "get_windows_autostart_manager",
            return_value=self.fake_manager,
            create=True,
        )
        self.manager_patch.start()

    async def asyncTearDown(self):
        self.manager_patch.stop()
        admin.active_admin_tokens.discard("admin-session-fixture")
        await self.client.aclose()

    async def test_status_and_toggle_require_admin_session(self):
        no_auth_get = await self.client.get("/api/admin/windows-autostart")
        no_auth_post = await self.client.post("/api/admin/windows-autostart", json={"enabled": True})
        self.assertEqual(401, no_auth_get.status_code)
        self.assertEqual(401, no_auth_post.status_code)

        headers = {"Authorization": "Bearer admin-session-fixture"}
        status = await self.client.get("/api/admin/windows-autostart", headers=headers)
        enabled = await self.client.post(
            "/api/admin/windows-autostart",
            headers=headers,
            json={"enabled": True},
        )
        self.assertEqual(200, status.status_code)
        self.assertEqual("disabled", status.json()["status"])
        self.assertEqual(200, enabled.status_code)
        self.assertEqual("enabled", enabled.json()["status"])
        self.assertEqual([True], self.fake_manager.calls)

        for extra_field in ("command", "path", "task_name", "powershell"):
            injected = await self.client.post(
                "/api/admin/windows-autostart",
                headers=headers,
                json={"enabled": False, extra_field: "forbidden-fixture"},
            )
            self.assertEqual(422, injected.status_code)
        self.assertEqual([True], self.fake_manager.calls)


class WindowsAutostartStaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manage_html = (REPO_ROOT / "static" / "manage.html").read_text(encoding="utf-8")
        cls.launcher_path = REPO_ROOT / "start-flow2api.pyw"

    def _load_launcher(self):
        self.assertTrue(self.launcher_path.exists(), "missing start-flow2api.pyw")
        loader = importlib.machinery.SourceFileLoader("start_flow2api_launcher", str(self.launcher_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_manage_page_exposes_real_windows_autostart_status_and_copy(self):
        html = self.manage_html
        self.assertIn("Windows 登录后自动启动 Flow2API", html)
        self.assertIn('id="cfgWindowsAutostart"', html)
        self.assertIn('id="windowsAutostartStatus"', html)
        self.assertIn('id="windowsAutostartReason"', html)
        self.assertIn("/api/admin/windows-autostart", html)
        self.assertIn("loadWindowsAutostart", html)
        self.assertIn("setWindowsAutostart", html)
        self.assertRegex(html, r"status\s*===\s*['\"]enabled['\"]")
        self.assertRegex(html, r"status\s*===\s*['\"]unsupported['\"]")
        self.assertRegex(html, r"JSON\.stringify\(\{\s*enabled\s*:")

    def test_cmd_launcher_delegates_to_existing_pyw_launcher(self):
        cmd = (REPO_ROOT / "start-flow2api.cmd").read_text(encoding="utf-8").lower()
        self.assertIn('"%~dp0venv\\scripts\\pythonw.exe"', cmd)
        self.assertIn('"%~dp0start-flow2api.pyw"', cmd)
        self.assertIn('start ""', cmd)
        self.assertNotIn("netstat", cmd)
        self.assertNotIn("pause", cmd)
        self.assertNotIn("main.py", cmd)
        for forbidden in ("authorization", "api_key", "cookie", "profile", "password", "token"):
            self.assertNotIn(forbidden, cmd)

    def test_one_click_launcher_deduplicates_when_health_is_already_ready(self):
        launcher = self._load_launcher()
        popen_calls = []
        opened = []
        errors = []

        result = launcher.run_launcher(
            health_probe=lambda url: url == launcher.HEALTH_URL,
            popen=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
            sleep=lambda _: None,
            monotonic=lambda: 0.0,
            open_browser=lambda url: opened.append(url),
            show_error=lambda message: errors.append(message),
        )

        self.assertEqual(0, result)
        self.assertEqual([], popen_calls)
        self.assertEqual([launcher.MANAGE_URL], opened)
        self.assertEqual([], errors)

    def test_one_click_launcher_uses_fixed_argv_cwd_and_waits_until_ready(self):
        launcher = self._load_launcher()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            python_exe = repo / "venv" / "Scripts" / "python.exe"
            python_exe.parent.mkdir(parents=True)
            python_exe.write_bytes(b"")
            (repo / "main.py").write_text("", encoding="utf-8")
            probe_results = iter((False, False, True))
            popen_calls = []
            sleeps = []
            opened = []
            errors = []

            def fake_popen(argv, **kwargs):
                popen_calls.append((list(argv), dict(kwargs)))
                return object()

            with patch.object(launcher, "REPO_ROOT", repo):
                result = launcher.run_launcher(
                    ensure_runtime=lambda _repo: python_exe,
                    health_probe=lambda url: next(probe_results),
                    popen=fake_popen,
                    sleep=lambda seconds: sleeps.append(seconds),
                    monotonic=iter((0.0, 0.0, 1.0, 2.0)).__next__,
                    open_browser=lambda url: opened.append(url),
                    show_error=lambda message: errors.append(message),
                    startup_timeout_seconds=10.0,
                )

        self.assertEqual(0, result)
        self.assertEqual(1, len(popen_calls))
        argv, kwargs = popen_calls[0]
        self.assertEqual([str(python_exe), str(repo / "main.py")], argv)
        self.assertEqual(str(repo), kwargs["cwd"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual([1.0], sleeps)
        self.assertEqual([launcher.MANAGE_URL], opened)
        self.assertEqual([], errors)

    def test_fresh_checkout_bootstraps_runtime_then_starts_service(self):
        launcher = self._load_launcher()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "main.py").write_text("", encoding="utf-8")
            (repo / "requirements.txt").write_text("fastapi==0.119.0\n", encoding="utf-8")
            python_exe = repo / "venv" / "Scripts" / "python.exe"
            ensure_calls = []
            popen_calls = []
            probe_results = iter((False, True))

            def fake_ensure_runtime(candidate_repo):
                ensure_calls.append(Path(candidate_repo))
                python_exe.parent.mkdir(parents=True)
                python_exe.write_bytes(b"")
                return python_exe

            result = launcher.run_launcher(
                repo_root=repo,
                ensure_runtime=fake_ensure_runtime,
                health_probe=lambda _url: next(probe_results),
                popen=lambda argv, **kwargs: popen_calls.append((list(argv), dict(kwargs))),
                sleep=lambda _seconds: None,
                monotonic=iter((0.0, 0.0, 1.0)).__next__,
                open_browser=lambda _url: None,
                show_error=lambda message: self.fail(message),
                startup_timeout_seconds=10.0,
            )

        self.assertEqual(0, result)
        self.assertEqual([repo], ensure_calls)
        self.assertEqual([str(python_exe), str(repo / "main.py")], popen_calls[0][0])

    def test_runtime_bootstrap_creates_venv_and_installs_changed_requirements_once(self):
        launcher = self._load_launcher()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            requirements = repo / "requirements.txt"
            requirements.write_text("fastapi==0.119.0\n", encoding="utf-8")
            calls = []

            def fake_run(argv, **kwargs):
                calls.append((list(argv), dict(kwargs)))
                if argv[1:3] == ["-m", "venv"]:
                    python_exe = repo / "venv" / "Scripts" / "python.exe"
                    python_exe.parent.mkdir(parents=True)
                    python_exe.write_bytes(b"")
                return SimpleNamespace(returncode=0)

            first = launcher.ensure_local_runtime(
                repo,
                base_python=Path("C:/Python311/python.exe"),
                run=fake_run,
            )
            second = launcher.ensure_local_runtime(
                repo,
                base_python=Path("C:/Python311/python.exe"),
                run=fake_run,
            )

        self.assertEqual(repo / "venv" / "Scripts" / "python.exe", first)
        self.assertEqual(first, second)
        self.assertEqual(2, len(calls))
        self.assertEqual(["-m", "venv"], calls[0][0][1:3])
        self.assertEqual(["-m", "pip", "install", "-r"], calls[1][0][1:5])

    def test_fresh_install_defaults_to_pluginless_personal_captcha(self):
        import tomli

        with (REPO_ROOT / "config" / "setting_example.toml").open("rb") as handle:
            defaults = tomli.load(handle)

        self.assertEqual("personal", defaults["captcha"]["captcha_method"])

    def test_one_click_launcher_reports_missing_runtime_and_timeout_without_opening_browser(self):
        launcher = self._load_launcher()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            errors = []
            opened = []
            with patch.object(launcher, "REPO_ROOT", repo):
                result = launcher.run_launcher(
                    health_probe=lambda _url: False,
                    popen=lambda *args, **kwargs: self.fail("must not start without repository venv"),
                    sleep=lambda _: None,
                    monotonic=lambda: 0.0,
                    open_browser=lambda url: opened.append(url),
                    show_error=lambda message: errors.append(message),
                )
            self.assertEqual(1, result)
            self.assertEqual([], opened)
            self.assertTrue(errors)

            python_exe = repo / "venv" / "Scripts" / "python.exe"
            python_exe.parent.mkdir(parents=True)
            python_exe.write_bytes(b"")
            (repo / "main.py").write_text("", encoding="utf-8")
            errors.clear()
            clock = iter((0.0, 0.0, 2.0)).__next__
            launches = []
            with patch.object(launcher, "REPO_ROOT", repo):
                result = launcher.run_launcher(
                    ensure_runtime=lambda _repo: python_exe,
                    health_probe=lambda _url: False,
                    popen=lambda argv, **kwargs: launches.append((list(argv), dict(kwargs))),
                    sleep=lambda _: None,
                    monotonic=clock,
                    open_browser=lambda url: opened.append(url),
                    show_error=lambda message: errors.append(message),
                    startup_timeout_seconds=1.0,
                )

        self.assertEqual(1, result)
        self.assertEqual(1, len(launches))
        self.assertEqual([], opened)
        self.assertTrue(any("就绪" in message or "启动" in message for message in errors))


if __name__ == "__main__":
    unittest.main()
