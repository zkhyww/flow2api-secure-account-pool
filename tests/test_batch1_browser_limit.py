import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.api import admin
from src.core.config import config
from src.core.database import Database
from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    _PersonalBrowserPoolService,
    resolve_effective_browser_count,
)


class _InputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return

        data = dict(attrs)
        element_id = data.get("id")
        if element_id in {
            "cfgBrowserCount",
            "cfgPersonalBrowserCount",
        }:
            self.inputs[element_id] = data


class Batch1BrowserLimitTests(unittest.TestCase):
    def test_config_and_resolver_normalize_browser_count(self):
        original = config.browser_count

        try:
            config.set_browser_count(20)
            self.assertEqual(config.browser_count, 10)

            config.set_browser_count(0)
            self.assertEqual(config.browser_count, 1)

            config.set_browser_count("bad")
            self.assertEqual(config.browser_count, 1)

            self.assertEqual(resolve_effective_browser_count(20), 10)
            self.assertEqual(resolve_effective_browser_count(0), 1)
            self.assertEqual(resolve_effective_browser_count("bad"), 1)
        finally:
            config.set_browser_count(original)


class Batch1AdminBrowserLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_normalizes_browser_count_without_runtime_prepare(self):
        fake_db = AsyncMock()
        fake_db.update_captcha_config = AsyncMock()
        fake_db.reload_config_to_memory = AsyncMock()

        with patch.object(admin, "db", fake_db), patch.object(
            admin,
            "_schedule_captcha_runtime_prepare",
            new=Mock(),
        ) as prepare:
            await admin.update_captcha_config(
                {
                    "browser_count": 20,
                    "captcha_method": "yescaptcha",
                },
                token="fixture",
            )

        kwargs = fake_db.update_captcha_config.await_args.kwargs
        self.assertEqual(kwargs["browser_count"], 10)
        fake_db.reload_config_to_memory.assert_awaited_once()
        prepare.assert_not_called()


class Batch1DatabaseBrowserLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_insert_and_update_normalize_browser_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "test.db")
            db = Database(db_path)
            await db.init_db()

            async with db._connect(write=True) as conn:
                await conn.execute("DELETE FROM captcha_config WHERE id=1")
                await conn.commit()

            await db.update_captcha_config(browser_count=20)

            inserted = await db.get_captcha_config()
            self.assertEqual(inserted.browser_count, 10)

            await db.update_captcha_config(browser_count=20)

            updated = await db.get_captcha_config()
            self.assertEqual(updated.browser_count, 10)


class Batch1HtmlBrowserLimitTests(unittest.TestCase):
    def test_manage_html_browser_inputs_have_max_ten(self):
        html_path = Path("static/manage.html")
        html = html_path.read_text(encoding="utf-8")

        parser = _InputParser()
        parser.feed(html)

        self.assertEqual(parser.inputs["cfgBrowserCount"]["max"], "10")
        self.assertEqual(parser.inputs["cfgPersonalBrowserCount"]["max"], "10")


class Batch1ResidentTabCapacityTests(unittest.TestCase):
    def test_resident_tab_capacity_is_independent_from_browser_limit(self):
        pool = _PersonalBrowserPoolService()

        limits = pool._build_worker_tab_limits(5, 10)

        self.assertEqual(len(limits), 10)
        self.assertEqual(sum(limits), 50)


class _FakeWorker:
    def __init__(self, worker_id):
        self.worker_id = worker_id
        self.apply_pool_worker_settings = Mock()
        self.reload_config = AsyncMock()
        self.close = AsyncMock()


class Batch1BrowserShrinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_shrink_workers_from_ten_to_three_closes_only_extra_workers(self):
        pool = _PersonalBrowserPoolService()

        workers = [_FakeWorker(index) for index in range(10)]

        pool._workers = workers
        pool._ensure_idle_worker_reaper = AsyncMock()
        pool._is_token_pool_enabled = Mock(return_value=False)

        with patch.object(
            BrowserCaptchaService,
            "_resolve_configured_browser_count",
            return_value=3,
        ):
            await pool._ensure_workers()

        self.assertEqual(len(pool._workers), 3)

        for worker in workers[:3]:
            worker.close.assert_not_awaited()

        for worker in workers[3:]:
            worker.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
