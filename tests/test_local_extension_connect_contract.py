import json
import re
import unittest
from pathlib import Path


class LocalExtensionConnectContractTests(unittest.TestCase):
    def test_localhost_content_relay_is_declared_and_does_not_expose_plugin_session(self):
        manifest = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))
        local_script = Path("extension/local-connect.js").read_text(encoding="utf-8")
        local_entry = next(
            entry for entry in manifest.get("content_scripts", [])
            if "local-connect.js" in entry.get("js", [])
        )
        self.assertEqual({"http://127.0.0.1/*", "http://localhost/*"}, set(local_entry["matches"]))
        for required in (
            "flow2api_local_connect_v1",
            "flow2api_local_connect_result_v1",
            "flow2api_local_connect_probe_v1",
            "flow2api_local_connect_ready_v1",
            "event.origin !== window.location.origin",
            "requestId",
            "flow2api_pair",
            "extension_upgrade_required",
        ):
            self.assertIn(required, local_script)
        self.assertNotIn("pluginSession", local_script)
        self.assertNotIn("google_cookies", local_script)

    def test_extension_startup_injects_relay_into_already_open_local_pages(self):
        """Loading/updating the unpacked extension must repair tabs opened earlier."""
        background = Path("extension/background.js").read_text(encoding="utf-8")
        local_script = Path("extension/local-connect.js").read_text(encoding="utf-8")

        self.assertIn("injectLocalConnectIntoOpenTabs", background)
        self.assertRegex(background, r"chrome\.tabs\.query\(")
        self.assertIn('files: ["local-connect.js"]', background)
        self.assertIsNotNone(re.search(
            r"injectLocalConnectIntoOpenTabs\(\).*?connectWS\(\)",
            background,
            re.DOTALL,
        ))
        self.assertIn("__flow2apiLocalConnectInstalled", local_script)

    def test_admin_pages_poll_status_and_use_one_hop_pairing_relay(self):
        for page in ("static/manage.html", "static/test.html"):
            html = Path(page).read_text(encoding="utf-8")
            for required in (
                'id="extensionConnectionStatus"',
                "/api/admin/extension-connection-status",
                "startExtensionConnectionPolling",
                "stopExtensionConnectionPolling",
                "visibilitychange",
            ):
                self.assertIn(required, html, page)

        manage = Path("static/manage.html").read_text(encoding="utf-8")
        self.assertIn("/api/admin/extension-pairing", manage)
        self.assertIn("flow2api_local_connect_v1", manage)
        self.assertIn("extension_upgrade_required", manage)
        self.assertIn("flow2api_local_connect_probe_v1", manage)
        self.assertIn("flow2api_local_connect_ready_v1", manage)
        self.assertIn("autoConnectExtensionFromPage", manage)
        self.assertIn("extensionRelayInstanceId", manage)
        self.assertIn("instance_id:window.extensionRelayInstanceId", manage)
        self.assertRegex(
            manage,
            r"DOMContentLoaded.*?probeExtensionRelay",
        )
        connect_function = re.search(
            r"connectExtensionFromPage=async\([^)]*\)=>\{.*?\},\n\s*autoConnectExtensionFromPage=",
            manage,
            re.DOTALL,
        )
        self.assertIsNotNone(connect_function)
        self.assertNotIn("pluginSession", connect_function.group(0))
        self.assertNotIn("google_cookies", connect_function.group(0))
        self.assertIn("flow2api_public_identity", Path("extension/background.js").read_text(encoding="utf-8"))
        self.assertIn("instanceId", Path("extension/local-connect.js").read_text(encoding="utf-8"))
        self.assertIn("account_bound", Path("extension/local-connect.js").read_text(encoding="utf-8"))
        self.assertIn(
            "extension_account_binding_pending",
            Path("src/api/admin.py").read_text(encoding="utf-8"),
        )
        self.assertIn("插件已连接，当前账号未绑定", manage)


if __name__ == "__main__":
    unittest.main()
