import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiosqlite
from fastapi import Request

from src.api import routes
from src.core.account_tiers import PAYGATE_TIER_NOT_PAID
from src.core.database import Database
from src.core.models import (
    ChatCompletionRequest,
    ChatMessage,
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiPart,
    Token,
)
from src.services.generation_handler import GenerationHandler


IMAGE_MODEL = "gemini-3.1-flash-image-landscape"
IDEMPOTENCY_HEADER = "Idempotency-Key"


class _SharedSubmitCounter:
    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()


class _CrossInstanceFlowClient:
    def __init__(self, shared):
        self.shared = shared

    def clear_request_fingerprint(self):
        return None

    async def prefill_remote_browser_pool(self, **kwargs):
        return None

    async def generate_image(self, **kwargs):
        self.shared.calls += 1
        self.shared.started.set()
        await self.shared.release.wait()
        return (
            {
                "media": [
                    {
                        "name": "media-ref",
                        "image": {"generatedImage": {"fifeUrl": "media-ref"}},
                    }
                ]
            },
            "session-ref",
            {},
        )


class _TokenManager:
    def __init__(self, token):
        self.token = token

    async def ensure_valid_token(self, token):
        return token

    async def ensure_project_exists(self, token_id):
        return "project-ref"

    async def record_usage(self, token_id, is_video=False):
        return None

    async def record_success(self, token_id):
        return None

    async def record_error(self, token_id):
        return None


class _LoadBalancer:
    def __init__(self, token):
        self.token = token

    async def select_token(self, **kwargs):
        return self.token

    async def release_pending(self, token_id, **kwargs):
        return None

    async def get_unavailable_reason(self, **kwargs):
        return None


class _ProxyManager:
    pass


async def _consume(handler, key):
    chunks = []
    async for chunk in handler.handle_generation(
        model=IMAGE_MODEL,
        prompt="fixture",
        images=None,
        stream=False,
        idempotency_key=key,
    ):
        chunks.append(chunk)
    return chunks


def _raw_request(path, idempotency_key=None):
    headers = []
    if idempotency_key:
        headers.append((b"idempotency-key", idempotency_key.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
        }
    )


class _CaptureHandler:
    def __init__(self):
        self.calls = []
        self.payload = '{"error":{"message":"fixture","status_code":400}}'
        self.chunks = None

    async def handle_generation(self, **kwargs):
        self.calls.append(kwargs)
        for chunk in self.chunks or [self.payload]:
            yield chunk


class Batch2CrossInstanceIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "shared.db"))
        await self.db.init_db()
        token_id = await self.db.add_token(
            Token(
                st="fixture-st",
                at="fixture-at",
                email="fixture@example.invalid",
                user_paygate_tier=PAYGATE_TIER_NOT_PAID,
                image_concurrency=-1,
                video_concurrency=-1,
            )
        )
        self.token = await self.db.get_token(token_id)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_two_handlers_share_database_atomic_get_or_create_and_submit_once(self):
        key = "cross-instance-key"
        shared = _SharedSubmitCounter()
        handler_a = GenerationHandler(
            _CrossInstanceFlowClient(shared),
            _TokenManager(self.token),
            _LoadBalancer(self.token),
            self.db,
            object(),
            _ProxyManager(),
        )
        handler_b = GenerationHandler(
            _CrossInstanceFlowClient(shared),
            _TokenManager(self.token),
            _LoadBalancer(self.token),
            self.db,
            object(),
            _ProxyManager(),
        )

        first = asyncio.create_task(_consume(handler_a, key))
        second = asyncio.create_task(_consume(handler_b, key))
        started = asyncio.create_task(shared.started.wait())
        done, _ = await asyncio.wait(
            {first, second, started},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=0.5,
        )
        try:
            for task in (first, second):
                if task in done and task.done() and task.exception() is not None:
                    await task
            self.assertIn(started, done, "first upstream submit never started")
            shared.release.set()
            left, right = await asyncio.gather(first, second)
        finally:
            if not started.done():
                started.cancel()
            for task in (first, second):
                if not task.done():
                    task.cancel()
            await asyncio.gather(first, second, started, return_exceptions=True)

        self.assertEqual(shared.calls, 1)
        self.assertEqual(left, right)
        async with self.db._connect() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
                (key,),
            )
            count = (await cursor.fetchone())[0]
        self.assertEqual(count, 1)


class Batch2PublicRouteIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_handler = routes.generation_handler
        self.capture = _CaptureHandler()
        routes.set_generation_handler(self.capture)

    async def asyncTearDown(self):
        routes.set_generation_handler(self.original_handler)

    async def test_openai_non_stream_passes_idempotency_key_to_generation_handler(self):
        request = ChatCompletionRequest(
            model=IMAGE_MODEL,
            messages=[ChatMessage(role="user", content="fixture")],
            stream=False,
        )
        raw = _raw_request("/v1/chat/completions", "route-openai-key")

        await routes.create_chat_completion(request, raw, api_key="fixture")

        self.assertEqual(len(self.capture.calls), 1)
        self.assertEqual(self.capture.calls[0].get("idempotency_key"), "route-openai-key")

    async def test_public_openai_idempotent_error_contract_does_not_expose_new_handler_status_or_code(self):
        self.capture.payload = json.dumps(
            {
                "error": {
                    "message": "model_access_denied",
                    "type": "invalid_request_error",
                    "code": "model_access_denied",
                    "status_code": 403,
                }
            }
        )
        request = ChatCompletionRequest(
            model=IMAGE_MODEL,
            messages=[ChatMessage(role="user", content="fixture")],
            stream=False,
        )

        response = await routes.create_chat_completion(
            request,
            _raw_request("/v1/chat/completions", "legacy-public-contract"),
            api_key="fixture",
        )
        payload = json.loads(response.body)

        self.assertEqual(502, response.status_code)
        self.assertEqual("generation_failed", payload["error"]["code"])
        self.assertEqual("server_error", payload["error"]["type"])

    async def test_public_openai_stream_error_event_keeps_legacy_idempotent_contract(self):
        self.capture.payload = json.dumps(
            {
                "error": {
                    "message": "model_access_denied",
                    "type": "invalid_request_error",
                    "code": "model_access_denied",
                    "status_code": 403,
                }
            }
        )
        request = ChatCompletionRequest(
            model=IMAGE_MODEL,
            messages=[ChatMessage(role="user", content="fixture")],
            stream=True,
        )

        response = await routes.create_chat_completion(
            request,
            _raw_request("/v1/chat/completions", "legacy-public-stream-contract"),
            api_key="fixture",
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        event_payload = json.loads(
            next(
                line[6:]
                for line in "".join(chunks).splitlines()
                if line.startswith("data: {")
            )
        )

        self.assertEqual("generation_failed", event_payload["error"]["code"])
        self.assertEqual(502, event_payload["error"]["status_code"])
        self.assertEqual("server_error", event_payload["error"]["type"])

    async def test_public_openai_non_idempotent_error_contract_remains_legacy_500(self):
        self.capture.payload = json.dumps(
            {
                "error": {
                    "message": "model_access_denied",
                    "type": "invalid_request_error",
                    "code": "model_access_denied",
                    "status_code": 403,
                }
            }
        )
        request = ChatCompletionRequest(
            model=IMAGE_MODEL,
            messages=[ChatMessage(role="user", content="fixture")],
            stream=False,
        )

        response = await routes.create_chat_completion(
            request,
            _raw_request("/v1/chat/completions"),
            api_key="fixture",
        )
        payload = json.loads(response.body)

        self.assertEqual(500, response.status_code)
        self.assertEqual("generation_failed", payload["error"]["code"])
        self.assertEqual("server_error", payload["error"]["type"])

    async def test_public_openai_old_generation_failed_502_is_500_without_idempotency_and_502_with_it(self):
        self.capture.payload = json.dumps(
            {
                "error": {
                    "message": "生成任务创建失败",
                    "type": "server_error",
                    "code": "generation_failed",
                    "status_code": 502,
                }
            },
            ensure_ascii=False,
        )
        request = ChatCompletionRequest(
            model=IMAGE_MODEL,
            messages=[ChatMessage(role="user", content="fixture")],
            stream=False,
        )

        non_idempotent = await routes.create_chat_completion(
            request,
            _raw_request("/v1/chat/completions"),
            api_key="fixture",
        )
        idempotent = await routes.create_chat_completion(
            request,
            _raw_request("/v1/chat/completions", "legacy-old-style-502"),
            api_key="fixture",
        )

        non_idempotent_payload = json.loads(non_idempotent.body)
        idempotent_payload = json.loads(idempotent.body)
        self.assertEqual(500, non_idempotent.status_code)
        self.assertEqual("generation_failed", non_idempotent_payload["error"]["code"])
        self.assertEqual(502, idempotent.status_code)
        self.assertEqual("generation_failed", idempotent_payload["error"]["code"])

    async def test_public_openai_idempotent_special_status_contracts_stay_400_402_409(self):
        request = ChatCompletionRequest(
            model=IMAGE_MODEL,
            messages=[ChatMessage(role="user", content="fixture")],
            stream=False,
        )
        for stable_class, expected_status in (
            ("content_policy", 400),
            ("quota_exhausted", 402),
            ("submission_uncertain", 409),
        ):
            with self.subTest(stable_class=stable_class):
                self.capture.payload = json.dumps(
                    {
                        "error": {
                            "message": stable_class,
                            "type": "invalid_request_error",
                            "code": stable_class,
                            "status_code": 499,
                        }
                    }
                )
                response = await routes.create_chat_completion(
                    request,
                    _raw_request("/v1/chat/completions", f"legacy-{stable_class}"),
                    api_key="fixture",
                )
                payload = json.loads(response.body)
                self.assertEqual(expected_status, response.status_code)
                self.assertEqual("generation_failed", payload["error"]["code"])

    async def test_public_openai_stream_suppresses_error_reason_frames_and_sanitizes_final_error(self):
        internal_class = "model_access_denied"
        upstream_marker = "UPSTREAM_PRIVATE_REASON"
        private_url = "https://private.example/full/path?secret=value"
        self.capture.chunks = [
            "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": f"错误: {internal_class}"},
                            "finish_reason": None,
                        }
                    ]
                },
                ensure_ascii=False,
            ) + "\n\n",
            "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "reasoning_content": f"错误: 视频生成失败: {upstream_marker} {private_url}"
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                ensure_ascii=False,
            ) + "\n\n",
            json.dumps(
                {
                    "error": {
                        "message": internal_class,
                        "type": "invalid_request_error",
                        "code": internal_class,
                        "status_code": 403,
                    }
                }
            ),
        ]
        request = ChatCompletionRequest(
            model=IMAGE_MODEL,
            messages=[ChatMessage(role="user", content="fixture")],
            stream=True,
        )

        public_response = await routes.create_chat_completion(
            request,
            _raw_request("/v1/chat/completions"),
            api_key="fixture",
        )
        public_chunks = []
        async for chunk in public_response.body_iterator:
            public_chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        public_text = "".join(public_chunks)

        self.assertIn("generation_failed", public_text)
        self.assertNotIn(internal_class, public_text)
        self.assertNotIn(upstream_marker, public_text)
        self.assertNotIn(private_url, public_text)
        self.assertNotIn("https://", public_text)

        test_response = await routes.create_test_chat_completion(
            request,
            _raw_request("/api/test/chat/completions"),
            capability="fixture-capability",
        )
        test_chunks = []
        async for chunk in test_response.body_iterator:
            test_chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        test_text = "".join(test_chunks)
        self.assertIn(internal_class, test_text)

    async def test_gemini_non_stream_passes_idempotency_key_to_generation_handler(self):
        request = GeminiGenerateContentRequest(
            contents=[
                GeminiContent(
                    role="user",
                    parts=[GeminiPart(text="fixture")],
                )
            ]
        )
        raw = _raw_request("/models/model:generateContent", "route-gemini-key")

        await routes.generate_content(
            IMAGE_MODEL,
            request,
            raw,
            api_key="fixture",
        )

        self.assertEqual(len(self.capture.calls), 1)
        self.assertEqual(self.capture.calls[0].get("idempotency_key"), "route-gemini-key")


class Batch2TaskMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_tasks_table_migrates_recovery_columns_and_unique_idempotency_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.db")
            db = Database(path)
            await db.init_db()

            async with db._connect(write=True) as conn:
                await conn.execute("DROP TABLE tasks")
                await conn.execute(
                    """
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT UNIQUE NOT NULL,
                        token_id INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'processing',
                        progress INTEGER DEFAULT 0,
                        result_urls TEXT,
                        error_message TEXT,
                        scene_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP
                    )
                    """
                )
                await conn.commit()

            await db.check_and_migrate_db({})

            async with db._connect() as conn:
                cursor = await conn.execute("PRAGMA table_info(tasks)")
                columns = {row[1] for row in await cursor.fetchall()}
            self.assertTrue(
                {"idempotency_key", "has_media", "error_class", "updated_at"}.issubset(columns)
            )

            async with db._connect(write=True) as conn:
                await conn.execute(
                    "INSERT INTO tasks (task_id, token_id, model, prompt, status, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("legacy-a", 1, "fixture-model", "", "created", "duplicate-key"),
                )
                await conn.commit()
                with self.assertRaises(aiosqlite.IntegrityError):
                    await conn.execute(
                        "INSERT INTO tasks (task_id, token_id, model, prompt, status, idempotency_key) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("legacy-b", 1, "fixture-model", "", "created", "duplicate-key"),
                    )


if __name__ == "__main__":
    unittest.main()
