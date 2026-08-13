import json
import re
import subprocess
import unittest
from pathlib import Path

from src.services.browser_captcha_extension import ExtensionCaptchaService


class _FakeWebSocket:
    def __init__(self):
        self.accepted_subprotocol = None
        self.sent = []

    async def accept(self, subprotocol=None):
        self.accepted_subprotocol = subprotocol

    async def send_text(self, value):
        self.sent.append(json.loads(value))


class ExtensionServerBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_message_cannot_overwrite_server_bound_identity(self):
        websocket = _FakeWebSocket()
        service = ExtensionCaptchaService()
        await service.connect(
            websocket,
            route_key="server-route",
            client_label="server-client",
            capability_marker="yingce-flow2api-worker-v1",
            subprotocol="flow2api-plugin",
        )

        await service.handle_message(
            websocket,
            json.dumps(
                {
                    "type": "register",
                    "route_key": "attacker-route",
                    "client_label": "attacker-client",
                }
            ),
        )

        connection = service._find_connection(websocket)
        self.assertEqual("server-route", connection.route_key)
        self.assertEqual("server-client", connection.client_label)
        self.assertEqual("yingce-flow2api-worker-v1", connection.capability_marker)
        self.assertEqual("server-route", websocket.sent[-1]["route_key"])


class CustomizedExtensionStaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.background = Path("extension/background.js").read_text(encoding="utf-8")
        cls.options = Path("extension/options.js").read_text(encoding="utf-8")
        cls.options_html = Path("extension/options.html").read_text(encoding="utf-8")
        cls.manifest = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))
        cls.all_extension_text = "\n".join(
            [
                cls.background,
                cls.options,
                cls.options_html,
                json.dumps(cls.manifest),
            ]
        )

    def test_customized_worker_has_capability_marker_and_account_import_permissions(self):
        self.assertIn("yingce-flow2api-worker-v1", self.all_extension_text)
        self.assertIn("cookies", self.manifest["permissions"])
        self.assertIn("alarms", self.manifest["permissions"])
        self.assertIn("flow2api_import_current_account", self.background)
        self.assertIn("autoImportEnabled", self.background)

    def test_global_api_key_is_not_stored_or_put_in_websocket_query(self):
        self.assertNotRegex(self.all_extension_text, r"\bapiKey\b")
        self.assertNotRegex(
            self.background,
            r"searchParams\.set\(\s*['\"](?:key|api_key)['\"]",
        )
        self.assertNotRegex(
            self.background,
            r"console\.(?:log|warn|error)\([^\n]*(?:url\.toString|serverUrl)",
        )

    def test_only_revocable_session_and_public_instance_metadata_are_settings(self):
        settings_match = re.search(
            r"const\s+DEFAULT_SETTINGS\s*=\s*\{(?P<body>.*?)\};",
            self.background,
            re.DOTALL,
        )
        self.assertIsNotNone(settings_match)
        body = settings_match.group("body")
        for required in ("pluginSession", "instanceId", "routeKey", "clientLabel"):
            self.assertIn(required, body)
        for forbidden in ("cookie", "password", "sessionToken", "accessToken"):
            self.assertNotIn(forbidden, body)

    def test_import_is_serialized_and_stale_socket_close_preserves_new_owner(self):
        self.assertRegex(self.background, r"importQueue\s*=\s*importQueue\.then")
        self.assertRegex(self.background, r"if\s*\(ws\s*===\s*socket\)")

    def test_failed_import_does_not_poison_later_serialized_imports(self):
        enqueue_match = re.search(
            r"function\s+enqueueImport\(reason\)\s*\{.*?^\}",
            self.background,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(enqueue_match, "background has no enqueueImport implementation")
        script = f"""
let importQueue = Promise.resolve();
let calls = 0;
let active = 0;
let maxActive = 0;
const chrome = {{ storage: {{ local: {{ set() {{}} }} }} }};
const console = {{ warn() {{}} }};
async function importCurrentAccount() {{
  calls += 1;
  active += 1;
  maxActive = Math.max(maxActive, active);
  await Promise.resolve();
  active -= 1;
  if (calls === 1) throw new Error('first_failed');
  return {{ calls }};
}}
{enqueue_match.group(0)}
(async () => {{
  const first = enqueueImport('first').catch(() => null);
  const second = enqueueImport('second');
  await Promise.all([first, second]);
  process.stdout.write(JSON.stringify({{ calls, maxActive }}));
}})().catch(error => {{ process.stderr.write(error.stack); process.exit(1); }});
"""
        result = subprocess.run(
            ["node", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"calls": 2, "maxActive": 1}, json.loads(result.stdout))

    def test_websocket_uses_plugin_session_subprotocol_not_query_auth(self):
        self.assertRegex(self.background, r"new\s+WebSocket\([^\n]+pluginSession")
        self.assertIn("flow2api-plugin", self.background)

    def test_options_page_makes_manual_pairing_an_advanced_fallback(self):
        self.assertIn('id="autoConnectBtn"', self.options_html)
        self.assertIn("openLocalManagement", self.options)
        self.assertIn("http://127.0.0.1:8000/manage", self.options)
        self.assertIn("<details", self.options_html)

    def test_pairing_automatically_binds_the_current_google_account_before_success(self):
        handler = re.search(
            r'if \(message\.type === "flow2api_pair"\) \{(?P<body>.*?)^    \}',
            self.background,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(handler)
        body = handler.group("body")
        self.assertIn('enqueueImport("pairing")', body)
        self.assertIn("account_bound", body)
        self.assertIn("account_binding_failed", body)
        self.assertLess(body.index('enqueueImport("pairing")'), body.index("success: true"))

    def test_captcha_worker_returns_token_with_exact_five_field_fingerprint(self):
        for field in (
            "user_agent",
            "accept_language",
            "sec_ch_ua",
            "sec_ch_ua_mobile",
            "sec_ch_ua_platform",
        ):
            self.assertIn(field, self.background)
        self.assertRegex(
            self.background,
            r"status:\s*['\"]success['\"],\s*token,\s*fingerprint",
        )
        self.assertIn("projectFlowUrl(data.project_id)", self.background)

    def test_captcha_worker_uses_same_verified_flow_project_page_as_backend(self):
        self.assertIn("https://labs.google/fx/tools/flow/project/", self.background)
        self.assertNotIn("https://labs.google/fx/projects/", self.background)


if __name__ == "__main__":
    unittest.main()
