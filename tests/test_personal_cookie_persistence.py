import base64
import json
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


class PersonalCookiePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.protector = _OpaqueCurrentUserProtector()
        self.protector_patch = patch(
            "src.core.database._get_google_cookies_protector",
            return_value=self.protector,
        )
        self.protector_patch.start()
        self.db = Database(str(Path(self.temp_dir.name) / "personal-cookies.db"))
        await self.db.init_db()
        self.service = BrowserCaptchaService(db=self.db)

    async def asyncTearDown(self):
        self.protector_patch.stop()
        self.temp_dir.cleanup()

    async def _add_token(self, *, st: str, google_cookies: str = "") -> int:
        return await self.db.add_token(
            Token(
                st=st,
                email="",
                protocol_mode="protocol",
                google_cookies=google_cookies,
            )
        )

    async def _raw_google_cookies(self, token_id: int) -> str:
        async with self.db._connect() as connection:
            cursor = await connection.execute(
                "SELECT google_cookies FROM tokens WHERE id = ?",
                (token_id,),
            )
            row = await cursor.fetchone()
        return str(row[0] or "")

    async def test_load_uses_selected_tokens_google_cookies(self):
        first_id = await self._add_token(
            st="opaque-session-a",
            google_cookies="SID=opaque-cookie-a",
        )
        await self._add_token(
            st="opaque-session-b",
            google_cookies="SID=opaque-cookie-b",
        )

        loaded = await self.service._load_token_cookie(first_id)

        self.assertEqual("SID=opaque-cookie-a", loaded)

    async def test_empty_selected_token_does_not_fall_back_to_another_account(self):
        await self._add_token(
            st="opaque-session-a",
            google_cookies="SID=opaque-cookie-a",
        )
        empty_id = await self._add_token(st="opaque-session-empty")

        loaded = await self.service._load_token_cookie(empty_id)

        self.assertIsNone(loaded)

    async def test_context_writeback_updates_google_cookies(self):
        token_id = await self._add_token(
            st="opaque-session-a",
            google_cookies=(
                '[{"name":"SID","value":"opaque-cookie-before",'
                '"domain":".google.com","path":"/","secure":true}]'
            ),
        )
        resident = ResidentTabInfo(
            tab=object(),
            slot_id="slot-fixture",
            token_id=token_id,
            browser_context_id="context-fixture",
        )
        self.service._get_browser_cookies = AsyncMock(
            return_value=[
                {
                    "name": "SID",
                    "value": "opaque-cookie-after",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                }
            ]
        )

        persisted = await self.service._persist_context_cookies_to_token(
            resident,
            token_id,
            label="unit",
        )

        loaded = await self.db.get_token(token_id)
        self.assertTrue(persisted)
        cookie_items = json.loads(loaded.google_cookies)
        self.assertEqual(
            ["opaque-cookie-after"],
            [item["value"] for item in cookie_items if item["name"] == "SID"],
        )

    async def test_context_writeback_remains_protected_in_raw_sqlite_cell(self):
        token_id = await self._add_token(st="opaque-session-a")
        resident = ResidentTabInfo(
            tab=object(),
            slot_id="slot-fixture",
            token_id=token_id,
            browser_context_id="context-fixture",
        )
        self.service._get_browser_cookies = AsyncMock(
            return_value=[
                {
                    "name": "SID",
                    "value": "opaque-cookie-after",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                }
            ]
        )

        persisted = await self.service._persist_context_cookies_to_token(
            resident,
            token_id,
            label="unit",
        )

        raw_cookie = await self._raw_google_cookies(token_id)
        self.assertTrue(persisted)
        self.assertTrue(raw_cookie.startswith(self.protector.marker))
        self.assertNotIn("opaque-cookie-after", raw_cookie)


if __name__ == "__main__":
    unittest.main()
