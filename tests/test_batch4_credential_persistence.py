import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.api import admin
from src.core.database import Database
from src.core.models import Token
from src.services import protocol_login
from src.services.token_manager import TokenManager


class _FakeCurrentUserDpapiProvider:
    """Deterministic stand-in for the Windows current-user DPAPI boundary."""

    marker = "dpapi-user:v1:"

    def is_protected(self, value: str) -> bool:
        return str(value or "").startswith(self.marker)

    def protect(self, value: str) -> str:
        raw = str(value or "").encode("utf-8")
        return self.marker + base64.urlsafe_b64encode(raw).decode("ascii")

    def unprotect(self, value: str) -> str:
        if not self.is_protected(value):
            raise ValueError("not a protected envelope")
        payload = str(value)[len(self.marker):]
        decoded = base64.b64decode(payload, altchars=b"-_", validate=True)
        return decoded.decode("utf-8")


class Batch4CredentialPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.provider = _FakeCurrentUserDpapiProvider()
        self.provider_patch = patch(
            "src.core.database._get_google_cookies_protector",
            return_value=self.provider,
            create=True,
        )
        self.provider_patch.start()
        self.db = Database(str(Path(self.temp_dir.name) / "credentials.db"))
        await self.db.init_db()

    async def asyncTearDown(self):
        self.provider_patch.stop()
        self.temp_dir.cleanup()

    async def _add_token(
        self,
        *,
        google_cookies: str = "",
        login_password: str = "",
        protocol_mode: str = "protocol",
    ) -> int:
        return await self.db.add_token(
            Token(
                st="session-placeholder",
                at=None,
                email="",
                protocol_mode=protocol_mode,
                google_cookies=google_cookies,
                login_password=login_password,
            )
        )

    async def _raw_credentials(self, token_id: int):
        async with self.db._connect() as conn:
            cursor = await conn.execute(
                "SELECT google_cookies, login_password FROM tokens WHERE id = ?",
                (token_id,),
            )
            return await cursor.fetchone()

    async def test_new_cookie_is_versioned_protected_at_rest_and_transparent_on_read(self):
        cookie_sentinel = "COOKIE_SENTINEL_ALPHA"

        token_id = await self._add_token(google_cookies=cookie_sentinel)
        raw_cookie, _ = await self._raw_credentials(token_id)
        loaded = await self.db.get_token(token_id)

        self.assertTrue(str(raw_cookie).startswith(self.provider.marker))
        self.assertNotIn(cookie_sentinel, str(raw_cookie))
        self.assertEqual(cookie_sentinel, loaded.google_cookies)

    async def test_legacy_plaintext_cookie_reads_and_migrates_on_next_cookie_update(self):
        legacy_sentinel = "COOKIE_SENTINEL_LEGACY"
        token_id = await self._add_token()
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                "UPDATE tokens SET google_cookies = ? WHERE id = ?",
                (legacy_sentinel, token_id),
            )
            await conn.commit()

        legacy = await self.db.get_token(token_id)
        await self.db.update_token(token_id, google_cookies=legacy.google_cookies)
        migrated_cookie, _ = await self._raw_credentials(token_id)
        migrated = await self.db.get_token(token_id)

        self.assertEqual(legacy_sentinel, legacy.google_cookies)
        self.assertTrue(str(migrated_cookie).startswith(self.provider.marker))
        self.assertNotIn(legacy_sentinel, str(migrated_cookie))
        self.assertEqual(legacy_sentinel, migrated.google_cookies)

    async def test_corrupt_protected_cookie_fails_closed_before_protocol_boundary(self):
        token_id = await self._add_token()
        corrupt_envelope = self.provider.marker + "%%%"
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                "UPDATE tokens SET google_cookies = ? WHERE id = ?",
                (corrupt_envelope, token_id),
            )
            await conn.commit()

        loaded = await self.db.get_token(token_id)
        manager = TokenManager(self.db, object())
        with patch.object(
            protocol_login.protocol_loginer,
            "login",
            new=AsyncMock(return_value={"success": False}),
        ) as login:
            refreshed = await manager._try_protocol_refresh_st(token_id, loaded)

        self.assertEqual("", loaded.google_cookies)
        self.assertIsNone(refreshed)
        login.assert_not_awaited()

    async def test_password_is_ignored_on_add_and_update(self):
        first_password_sentinel = "PASSWORD_SENTINEL_ADD"
        second_password_sentinel = "PASSWORD_SENTINEL_UPDATE"

        token_id = await self._add_token(login_password=first_password_sentinel)
        _, raw_after_add = await self._raw_credentials(token_id)
        await self.db.update_token(token_id, login_password=second_password_sentinel)
        _, raw_after_update = await self._raw_credentials(token_id)

        self.assertEqual("", raw_after_add or "")
        self.assertEqual("", raw_after_update or "")

    async def test_legacy_password_is_hidden_from_business_read_and_admin_api(self):
        legacy_password_sentinel = "PASSWORD_SENTINEL_LEGACY"
        token_id = await self._add_token()
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                "UPDATE tokens SET login_password = ? WHERE id = ?",
                (legacy_password_sentinel, token_id),
            )
            await conn.commit()

        manager = TokenManager(self.db, object())
        loaded = await manager.get_token(token_id)
        with patch.object(admin, "db", self.db):
            payload = await admin.get_tokens(token="fixture")

        self.assertEqual("", loaded.login_password)
        self.assertEqual("", payload[0].get("login_password", ""))


if __name__ == "__main__":
    unittest.main()
