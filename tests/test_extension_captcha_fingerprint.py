import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.browser_captcha_extension import (
    ExtensionCaptchaBundle,
    ExtensionCaptchaService,
)
from src.services.flow_client import FlowClient


FINGERPRINT = {
    "user_agent": "browser-fixture",
    "accept_language": "en-US,en;q=0.9",
    "sec_ch_ua": '"Chromium";v="140"',
    "sec_ch_ua_mobile": "?0",
    "sec_ch_ua_platform": '"Windows"',
}


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def accept(self, subprotocol=None):
        self.subprotocol = subprotocol

    async def send_text(self, value):
        self.sent.append(json.loads(value))


class _RouteDatabase:
    async def get_token(self, token_id):
        return SimpleNamespace(extension_route_key=f"route-{token_id}")


class ExtensionCaptchaFingerprintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.websocket = _FakeWebSocket()
        self.service = ExtensionCaptchaService(db=_RouteDatabase())
        await self.service.connect(
            self.websocket,
            route_key="route-7",
            client_label="worker",
            capability_marker="yingce-flow2api-worker-v1",
        )

    async def _request_and_respond(self, response_patch):
        task = asyncio.create_task(
            self.service.get_token(
                "project-fixture",
                token_id=7,
                session_id="generation-context",
            )
        )
        while not self.websocket.sent:
            await asyncio.sleep(0)
        request = self.websocket.sent[-1]
        response = {
            "req_id": request["req_id"],
            "status": "success",
            "token": "captcha-fixture",
            "fingerprint": dict(FINGERPRINT),
            **response_patch,
        }
        await self.service.handle_message(self.websocket, json.dumps(response))
        return request, await task

    async def test_bundle_is_strictly_allowlisted_and_server_context_is_authoritative(self):
        request, bundle = await self._request_and_respond(
            {
                "route_key": "attacker-route",
                "project_id": "attacker-project",
                "session_id": "attacker-context",
            }
        )
        self.assertEqual("route-7", request["route_key"])
        self.assertEqual("project-fixture", request["project_id"])
        self.assertEqual("generation-context", request["session_id"])
        self.assertIsInstance(bundle, ExtensionCaptchaBundle)
        self.assertEqual("captcha-fixture", bundle.token)
        self.assertEqual(FINGERPRINT, bundle.fingerprint)
        self.assertFalse(hasattr(bundle, "route_key"))
        self.assertFalse(hasattr(bundle, "project_id"))
        self.assertFalse(hasattr(bundle, "session_id"))

    async def test_unknown_fingerprint_key_rejects_entire_bundle(self):
        fingerprint = dict(FINGERPRINT)
        fingerprint["cookie"] = "must-not-pass"
        _, bundle = await self._request_and_respond({"fingerprint": fingerprint})
        self.assertIsNone(bundle)

    async def test_extension_bundle_fingerprint_reaches_flow_request_headers(self):
        flow = FlowClient(proxy_manager=None)
        service = AsyncMock()
        service.get_token.return_value = ExtensionCaptchaBundle(
            token="captcha-fixture",
            fingerprint=dict(FINGERPRINT),
        )
        response = MagicMock(status_code=200, headers={}, text="{}")
        response.json.return_value = {"ok": True}
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.__aexit__.return_value = False
        session.post.return_value = response

        with patch("src.services.flow_client.config") as cfg, patch(
            "src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance",
            new=AsyncMock(return_value=service),
        ), patch("src.services.flow_client.AsyncSession", return_value=session):
            cfg.captcha_method = "extension"
            cfg.debug_enabled = False
            token, browser_id = await flow._get_recaptcha_token(
                "project-fixture",
                token_id=7,
                session_id="generation-context",
            )
            self.assertEqual("captcha-fixture", token)
            self.assertIsNone(browser_id)
            await flow._make_request(
                "POST",
                "https://example.invalid/flow/upsampleImage",
                json_data={"fixture": True},
            )

        sent_headers = session.post.await_args.kwargs["headers"]
        self.assertEqual("browser-fixture", sent_headers["User-Agent"])
        self.assertEqual("en-US,en;q=0.9", sent_headers["Accept-Language"])
        self.assertEqual('?0', sent_headers["sec-ch-ua-mobile"])
        service.get_token.assert_awaited_once_with(
            "project-fixture",
            "IMAGE_GENERATION",
            timeout=25,
            token_id=7,
            session_id="generation-context",
        )


if __name__ == "__main__":
    unittest.main()
