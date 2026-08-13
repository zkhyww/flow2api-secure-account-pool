import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services import browser_captcha_personal as personal


class PersonalRuntimeOrphanCleanupContractTests(unittest.IsolatedAsyncioTestCase):
    def test_user_data_dir_parser_requires_an_exact_switch_token(self):
        managed = str(personal.PERSONAL_RUNTIME_TMP_DIR / "browser_profile_parser")
        extract = personal._extract_command_line_switch_value

        self.assertEqual(
            managed,
            extract(f'chrome.exe --user-data-dir="{managed}" --flag', "--user-data-dir"),
        )
        self.assertEqual(
            managed,
            extract(f"chrome.exe --USER-DATA-DIR={managed} --flag", "--user-data-dir"),
        )
        self.assertIsNone(
            extract(f'chrome.exe --user-data-dir="{managed}"junk --flag', "--user-data-dir")
        )
        self.assertIsNone(
            extract(f'chrome.exe --note=--user-data-dir="{managed}"', "--user-data-dir")
        )

    def test_selects_only_repo_managed_orphans_with_dead_owner(self):
        select_roots = getattr(personal, "_select_stale_personal_browser_roots", None)
        self.assertIsNotNone(select_roots, "missing stale personal browser ownership selector")

        owned_old = str(personal.PERSONAL_RUNTIME_TMP_DIR / "browser_profile_old-a")
        owned_live = str(personal.PERSONAL_RUNTIME_TMP_DIR / "browser_profile_live")
        owned_current = str(personal.PERSONAL_RUNTIME_TMP_DIR / "browser_profile_current")
        owned_second = str(personal.PERSONAL_RUNTIME_TMP_DIR / "fresh_browser_profile_old-b")
        external_profile = r"C:\Users\Public\Chrome\User Data"
        live_pids = {22, 999}
        records = [
            {"pid": 101, "parent_pid": 11, "name": "chrome.exe", "command_line": f'chrome.exe --user-data-dir="{owned_old}"'},
            {"pid": 102, "parent_pid": 22, "name": "chrome.exe", "command_line": f'chrome.exe --user-data-dir="{owned_live}"'},
            {"pid": 103, "parent_pid": 33, "name": "chrome.exe", "command_line": f'chrome.exe --user-data-dir="{external_profile}" --note="{owned_old}"'},
            {"pid": 104, "parent_pid": 44, "name": "chrome.exe", "command_line": f'chrome.exe --user-data-dir="{owned_current}"'},
            {"pid": 105, "parent_pid": 55, "name": "msedge.exe", "command_line": f'msedge.exe --user-data-dir="{owned_second}"'},
        ]

        selected = select_roots(
            records,
            active_runtime_paths={owned_current},
            current_pid=999,
            pid_is_running=lambda pid: pid in live_pids,
        )

        self.assertEqual([101, 105], selected)

    def test_missing_parent_metadata_fails_closed(self):
        select_roots = personal._select_stale_personal_browser_roots
        owned = str(personal.PERSONAL_RUNTIME_TMP_DIR / "browser_profile_missing-parent")
        records = [
            {
                "pid": 111,
                "parent_pid": None,
                "name": "chrome.exe",
                "command_line": f'chrome.exe --user-data-dir="{owned}"',
            }
        ]

        selected = select_roots(
            records,
            active_runtime_paths=set(),
            current_pid=999,
            pid_is_running=lambda _pid: False,
        )

        self.assertEqual([], selected)

    async def test_cleanup_metadata_scan_failure_is_fail_closed_and_non_blocking(self):
        cleanup = personal.BrowserCaptchaService.cleanup_stale_runtime_artifacts
        with tempfile.TemporaryDirectory() as tmp:
            runtime_tmp = Path(tmp) / "tmp"
            runtime_data = Path(tmp) / "data"
            (runtime_tmp / "browser_profile_old-scan").mkdir(parents=True)
            with patch.object(personal, "PERSONAL_RUNTIME_TMP_DIR", runtime_tmp), patch.object(
                personal,
                "PERSONAL_RUNTIME_DATA_DIR",
                runtime_data,
            ), patch.object(
                personal.BrowserCaptchaService,
                "_instance",
                None,
            ), patch.object(
                personal.BrowserCaptchaService,
                "_pool_instance",
                None,
            ), patch.object(
                personal.BrowserCaptchaService,
                "_find_browser_pids_for_profile_dirs",
                side_effect=RuntimeError("synthetic metadata failure"),
            ), patch.object(
                personal.BrowserCaptchaService,
                "_terminate_pid_tree",
            ) as tree_reaper, patch.object(
                personal,
                "_cleanup_runtime_artifacts_sync",
                return_value={
                    "profiles_deleted": 0,
                    "recaptcha_cache_deleted": 0,
                    "proxy_extensions_deleted": 0,
                },
            ):
                stats = await cleanup(reason="startup")

        tree_reaper.assert_not_called()
        self.assertEqual(0, stats["orphan_process_groups_terminated"])

    async def test_cleanup_routes_only_selected_orphan_roots_to_existing_pid_tree_reaper(self):
        cleanup = getattr(personal.BrowserCaptchaService, "cleanup_stale_runtime_artifacts")
        with tempfile.TemporaryDirectory() as tmp:
            runtime_tmp = Path(tmp) / "tmp"
            runtime_data = Path(tmp) / "data"
            (runtime_tmp / "browser_profile_old-c").mkdir(parents=True)
            with patch.object(personal, "PERSONAL_RUNTIME_TMP_DIR", runtime_tmp), patch.object(
                personal,
                "PERSONAL_RUNTIME_DATA_DIR",
                runtime_data,
            ), patch.object(
                personal.BrowserCaptchaService,
                "_instance",
                None,
            ), patch.object(
                personal.BrowserCaptchaService,
                "_pool_instance",
                None,
            ), patch.object(
                personal.BrowserCaptchaService,
                "_find_browser_pids_for_profile_dirs",
                return_value=[201],
            ) as stale_finder, patch.object(
                personal.BrowserCaptchaService,
                "_terminate_pid_tree",
                return_value=True,
            ) as tree_reaper, patch.object(
                personal,
                "_cleanup_runtime_artifacts_sync",
                return_value={
                    "profiles_deleted": 0,
                    "recaptcha_cache_deleted": 0,
                    "proxy_extensions_deleted": 0,
                },
            ):
                stats = await cleanup(reason="startup")

        stale_finder.assert_called_once()
        finder_kwargs = stale_finder.call_args.kwargs
        self.assertTrue(finder_kwargs["stale_orphans_only"])
        self.assertEqual(set(), finder_kwargs["active_runtime_paths"])
        tree_reaper.assert_called_once_with(201, reason="startup:stale_orphan")
        self.assertEqual(1, stats["orphan_process_groups_terminated"])
