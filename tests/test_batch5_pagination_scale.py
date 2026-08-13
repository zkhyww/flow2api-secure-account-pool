import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from src.api import admin
from src.core.database import Database
from src.services import protocol_login
from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    _PersonalBrowserPoolService,
)
from src.services.concurrency_manager import ConcurrencyManager


class _CountingConnection:
    def __init__(self, connection, owner):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_owner", owner)

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    async def execute(self, sql, parameters=None):
        statement = str(sql or "").lstrip().upper()
        if statement.startswith("SELECT") or statement.startswith("WITH"):
            self._owner.select_query_count += 1
        return await self._connection.execute(sql, parameters or ())

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _CountingDatabase(Database):
    decode_call_count = 0

    def __init__(self, db_path):
        super().__init__(db_path)
        self.select_query_count = 0

    @classmethod
    def _decode_token_row(cls, row):
        cls.decode_call_count += 1
        return super()._decode_token_row(row)

    @asynccontextmanager
    async def _connect(self, *, write=False):
        async with super()._connect(write=write) as connection:
            yield _CountingConnection(connection, self)

    def reset_observation_counts(self):
        self.select_query_count = 0
        type(self).decode_call_count = 0


class Batch5PaginationScaleTests(unittest.IsolatedAsyncioTestCase):
    async def _make_db(self):
        temp_dir = tempfile.TemporaryDirectory()
        db = _CountingDatabase(str(Path(temp_dir.name) / "batch5-scale.db"))
        await db.init_db()
        return temp_dir, db

    @staticmethod
    async def _seed_accounts(db, count):
        if count <= 0:
            return
        token_rows = [
            (
                token_id,
                f"seed-{token_id:04d}",
                "",
                f"account-{token_id:04d}",
                1,
                token_id % 11,
                1,
                1,
                "2026-01-01 00:00:00",
                "",
                "",
            )
            for token_id in range(1, count + 1)
        ]
        stat_rows = [(token_id, token_id % 3, token_id % 5) for token_id in range(1, count + 1)]
        async with db._connect(write=True) as conn:
            await conn.executemany(
                """
                INSERT INTO tokens (
                    id, st, email, name, is_active, credits,
                    image_enabled, video_enabled, created_at,
                    google_cookies, login_password
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                token_rows,
            )
            await conn.executemany(
                """
                INSERT INTO token_stats (token_id, image_count, video_count)
                VALUES (?, ?, ?)
                """,
                stat_rows,
            )
            await conn.commit()

    async def _database_page(self, db, *, limit, offset):
        self.assertTrue(
            hasattr(db, "get_tokens_page_with_stats"),
            "Database lacks the bounded Batch 5 account page query",
        )
        return await db.get_tokens_page_with_stats(limit=limit, offset=offset)

    async def _http_client_for(self, db):
        original_db = admin.db
        original_concurrency = admin.concurrency_manager
        admin.db = db
        admin.concurrency_manager = ConcurrencyManager()
        token = "batch5-scale-admin"
        admin.active_admin_tokens.add(token)
        app = FastAPI()
        app.include_router(admin.router)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}"},
        )
        return client, token, original_db, original_concurrency

    async def test_database_page_contract_handles_zero_and_one_account(self):
        for count in (0, 1):
            with self.subTest(account_count=count):
                temp_dir, db = await self._make_db()
                try:
                    await self._seed_accounts(db, count)
                    db.reset_observation_counts()
                    page = await self._database_page(db, limit=25, offset=0)

                    self.assertEqual(
                        {"items", "total", "limit", "offset", "has_next"},
                        set(page),
                    )
                    self.assertEqual(count, page["total"])
                    self.assertEqual(count, len(page["items"]))
                    self.assertEqual(25, page["limit"])
                    self.assertEqual(0, page["offset"])
                    self.assertFalse(page["has_next"])
                    self.assertLessEqual(db.select_query_count, 2)
                    self.assertLessEqual(db.decode_call_count, count)
                finally:
                    temp_dir.cleanup()

    async def test_two_hundred_accounts_page_without_duplicates_or_loss(self):
        temp_dir, db = await self._make_db()
        try:
            await self._seed_accounts(db, 200)
            db.reset_observation_counts()
            seen_ids = []
            page_calls = 0
            offset = 0

            while True:
                page = await self._database_page(db, limit=37, offset=offset)
                page_calls += 1
                self.assertEqual(200, page["total"])
                seen_ids.extend(item["id"] for item in page["items"])
                if not page["has_next"]:
                    break
                offset += page["limit"]

            self.assertEqual(list(range(200, 0, -1)), seen_ids)
            self.assertEqual(200, len(set(seen_ids)))
            self.assertEqual(200, db.decode_call_count)
            self.assertLessEqual(db.select_query_count, page_calls * 2)
        finally:
            temp_dir.cleanup()

    async def test_five_hundred_account_single_page_is_bounded_and_does_not_login_or_launch(self):
        temp_dir, db = await self._make_db()
        pool = _PersonalBrowserPoolService(db)
        client = None
        token = None
        original_db = None
        original_concurrency = None
        try:
            await self._seed_accounts(db, 500)
            db.reset_observation_counts()
            client, token, original_db, original_concurrency = await self._http_client_for(db)

            with patch.object(
                BrowserCaptchaService,
                "get_instance",
                new=AsyncMock(return_value=pool),
            ), patch.object(
                BrowserCaptchaService,
                "initialize",
                new=AsyncMock(),
            ) as initialize, patch.object(
                protocol_login.protocol_loginer,
                "login",
                new=AsyncMock(),
            ) as login:
                response = await client.get("/api/tokens?page=1&page_size=25")

            self.assertEqual(200, response.status_code)
            payload = response.json()
            items = payload.get("items", []) if isinstance(payload, dict) else payload
            with self.subTest(bound_return_objects=True):
                self.assertLessEqual(len(items), 25)
            with self.subTest(bound_decode_calls=True):
                self.assertLessEqual(db.decode_call_count, 25)
            with self.subTest(bound_select_queries=True):
                self.assertLessEqual(db.select_query_count, 3)
            with self.subTest(paginated_envelope=True):
                self.assertIsInstance(payload, dict)
                self.assertEqual(500, payload["total"])
                self.assertTrue(payload["has_next"])
            initialize.assert_not_awaited()
            login.assert_not_awaited()
        finally:
            if client is not None:
                await client.aclose()
            if token is not None:
                admin.active_admin_tokens.discard(token)
            if client is not None:
                admin.db = original_db
                admin.concurrency_manager = original_concurrency
            temp_dir.cleanup()

    async def test_page_size_is_clamped_but_total_is_not_hard_limited_to_two_hundred(self):
        temp_dir, db = await self._make_db()
        pool = _PersonalBrowserPoolService(db)
        client = None
        token = None
        original_db = None
        original_concurrency = None
        try:
            await self._seed_accounts(db, 500)
            client, token, original_db, original_concurrency = await self._http_client_for(db)
            with patch.object(
                BrowserCaptchaService,
                "get_instance",
                new=AsyncMock(return_value=pool),
            ), patch.object(
                BrowserCaptchaService,
                "initialize",
                new=AsyncMock(),
            ) as initialize:
                response = await client.get("/api/tokens?page=2&page_size=1000")

            self.assertEqual(200, response.status_code)
            payload = response.json()
            self.assertIsInstance(payload, dict, "explicit pagination must return an envelope")
            self.assertEqual(500, payload["total"])
            self.assertEqual(2, payload["page"])
            self.assertEqual(100, payload["page_size"])
            self.assertEqual(100, len(payload["items"]))
            self.assertEqual(list(range(400, 300, -1)), [item["id"] for item in payload["items"]])
            self.assertTrue(payload["has_next"])
            initialize.assert_not_awaited()
        finally:
            if client is not None:
                await client.aclose()
            if token is not None:
                admin.active_admin_tokens.discard(token)
            if client is not None:
                admin.db = original_db
                admin.concurrency_manager = original_concurrency
            temp_dir.cleanup()

    async def test_direct_call_without_pagination_keeps_legacy_list_shape_for_frozen_callers(self):
        temp_dir, db = await self._make_db()
        original_db = admin.db
        try:
            await self._seed_accounts(db, 1)
            admin.db = db
            payload = await admin.get_tokens(token="fixture")

            self.assertIsInstance(payload, list)
            self.assertEqual(1, len(payload))
        finally:
            admin.db = original_db
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
