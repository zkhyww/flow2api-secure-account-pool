import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.core.database import Database
from src.core.models import Token
from src.services.browser_captcha_personal import BrowserCaptchaService, ResidentTabInfo


class _OpaqueCurrentUserProtector:
    marker = "dpapi-user:v1:"

    def is_protected(self, value: str) -> bool:
        return str(value or "").startswith(self.marker)

    def protect(self, value: str) -> str:
        payload = str(value or "").encode("utf-8")
        return self.marker + base64.urlsafe_b64encode(payload).decode("ascii")

    def unprotect(self, value: str) -> str:
        if not self.is_protected(value):
            raise ValueError("not a protected envelope")
        payload = str(value)[len(self.marker):]
        return base64.b64decode(payload, altchars=b"-_", validate=True).decode("utf-8")


class PersonalRestartRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.protector_patch = patch(
            "src.core.database._get_google_cookies_protector",
            return_value=_OpaqueCurrentUserProtector(),
        )
        self.protector_patch.start()
        self.db = Database(str(Path(self.temp_dir.name) / "restart-restore.db"))
        await self.db.init_db()
        self.token_id = await self.db.add_token(
            Token(
                st="opaque-session-restart",
                email="",
                protocol_mode="protocol",
                google_cookies="SID=opaque-cookie-restart",
            )
        )

    async def asyncTearDown(self):
        self.protector_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _install_cookie_boundary(service):
        service._set_browser_cookie_targets = AsyncMock(return_value=1)
        service._tab_reload = AsyncMock()

    async def test_fresh_service_restores_selected_account_without_pairing_state(self):
        first_service = BrowserCaptchaService(db=self.db)
        self._install_cookie_boundary(first_service)
        first_resident = ResidentTabInfo(
            tab=object(),
            slot_id="slot-before-restart",
            browser_context_id="context-before-restart",
        )

        first_ok = await first_service._apply_token_cookie_binding(
            first_resident,
            self.token_id,
            label="before-restart",
        )

        restarted_service = BrowserCaptchaService(db=self.db)
        self.assertEqual({}, restarted_service._token_resident_affinity)
        self.assertFalse(hasattr(restarted_service, "pairing_service"))
        self._install_cookie_boundary(restarted_service)
        restarted_resident = ResidentTabInfo(
            tab=object(),
            slot_id="slot-after-restart",
            browser_context_id="context-after-restart",
        )
        restarted_ok = await restarted_service._apply_token_cookie_binding(
            restarted_resident,
            self.token_id,
            label="after-restart",
        )

        first_call = first_service._set_browser_cookie_targets.await_args
        restarted_call = restarted_service._set_browser_cookie_targets.await_args
        self.assertTrue(first_ok)
        self.assertTrue(restarted_ok)
        self.assertEqual(
            {"opaque-cookie-restart"},
            {item["value"] for item in first_call.args[0]},
        )
        self.assertEqual(
            {"opaque-cookie-restart"},
            {item["value"] for item in restarted_call.args[0]},
        )
        self.assertEqual(
            "context-before-restart",
            first_call.kwargs["browser_context_id"],
        )
        self.assertEqual(
            "context-after-restart",
            restarted_call.kwargs["browser_context_id"],
        )


if __name__ == "__main__":
    unittest.main()
