import asyncio
import base64
import json
import unittest
from unittest.mock import patch

import httpx

from src.api import yingce_adapter
from src.core.config import config
from src.main import app, generation_handler


class YingceMultipartRequestLimitSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_api_key = config.api_key
        config.api_key = "yingce-synthetic-api-key"
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        config.api_key = self.original_api_key
        await self.client.aclose()

    def _auth_headers(self):
        return {"Authorization": "Bearer yingce-synthetic-api-key"}

    def _capture_generation(self, chunks):
        original = generation_handler.handle_generation

        async def fake_handle_generation(**kwargs):
            for chunk in chunks:
                yield chunk

        generation_handler.handle_generation = fake_handle_generation
        self.addCleanup(setattr, generation_handler, "handle_generation", original)

    async def test_complete_body_replay_preserves_followup_disconnect(self):
        received_by_downstream = []
        receive_messages = [
            {"type": "http.request", "body": b"synthetic-body", "more_body": False},
            {"type": "http.disconnect"},
        ]

        async def receive():
            return receive_messages.pop(0)

        async def downstream(scope, replay_receive, send):
            received_by_downstream.append(await replay_receive())
            received_by_downstream.append(await replay_receive())

        middleware = yingce_adapter.YingceMultipartBodyLimitMiddleware(downstream)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/images/edits",
            "headers": [],
        }

        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", 1024):
            await asyncio.wait_for(middleware(scope, receive, lambda message: None), timeout=0.2)

        self.assertEqual("http.request", received_by_downstream[0]["type"])
        self.assertEqual(b"synthetic-body", received_by_downstream[0]["body"])
        self.assertEqual("http.disconnect", received_by_downstream[1]["type"])
        self.assertEqual([], receive_messages)

    async def test_disconnect_before_complete_body_never_invokes_downstream(self):
        downstream_calls = 0
        sent_messages = []
        receive_messages = [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]

        async def receive():
            return receive_messages.pop(0)

        async def send(message):
            sent_messages.append(message)

        async def downstream(scope, replay_receive, send):
            nonlocal downstream_calls
            downstream_calls += 1

        middleware = yingce_adapter.YingceMultipartBodyLimitMiddleware(downstream)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/videos",
            "headers": [],
        }

        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", 1024):
            await asyncio.wait_for(middleware(scope, receive, send), timeout=0.2)

        self.assertEqual(0, downstream_calls)
        self.assertEqual([], sent_messages)
        self.assertEqual([], receive_messages)

    async def test_oversize_stream_sends_one_response_and_never_invokes_downstream(self):
        downstream_calls = 0
        sent_messages = []
        receive_calls = 0
        receive_messages = [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56789", "more_body": True},
            {"type": "http.request", "body": b"must-not-be-read", "more_body": False},
        ]

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            return receive_messages.pop(0)

        async def send(message):
            sent_messages.append(message)

        async def downstream(scope, replay_receive, send):
            nonlocal downstream_calls
            downstream_calls += 1

        middleware = yingce_adapter.YingceMultipartBodyLimitMiddleware(downstream)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/images/edits",
            "headers": [],
        }

        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", 8):
            await asyncio.wait_for(middleware(scope, receive, send), timeout=0.2)

        self.assertEqual(0, downstream_calls)
        self.assertEqual(2, receive_calls)
        self.assertEqual(1, sum(message["type"] == "http.response.start" for message in sent_messages))
        self.assertEqual(1, sum(message["type"] == "http.response.body" for message in sent_messages))
        self.assertEqual(413, next(message["status"] for message in sent_messages if message["type"] == "http.response.start"))
        self.assertEqual(1, len(receive_messages))

    async def test_in_limit_body_finishes_when_downstream_does_not_read_replay(self):
        downstream_calls = 0
        receive_messages = [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]

        async def receive():
            return receive_messages.pop(0)

        async def downstream(scope, replay_receive, send):
            nonlocal downstream_calls
            downstream_calls += 1

        middleware = yingce_adapter.YingceMultipartBodyLimitMiddleware(downstream)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/videos",
            "headers": [],
        }

        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", 8):
            await asyncio.wait_for(middleware(scope, receive, lambda message: None), timeout=0.2)

        self.assertEqual(1, downstream_calls)
        self.assertEqual([], receive_messages)

    async def test_video_multiple_references_are_limited_by_total_request_size(self):
        limit = 512
        request = self.client.build_request(
            "POST",
            "/v1/videos",
            headers=self._auth_headers(),
            data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
            files=[
                ("input_reference[]", ("one.png", b"a" * 160, "image/png")),
                ("input_reference[]", ("two.png", b"b" * 160, "image/png")),
            ],
        )
        self.assertGreater(int(request.headers["Content-Length"]), limit)
        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", limit, create=True):
            with patch.object(yingce_adapter, "MAX_UPLOAD_BYTES", 4096):
                response = await self.client.send(request)
        self.assertEqual(413, response.status_code)
        self.assertEqual("media_too_large", response.json()["error"]["code"])

    async def test_total_limit_does_not_apply_to_generations_or_official_routes(self):
        encoded = base64.b64encode(b"synthetic-output").decode("ascii")
        self._capture_generation([
            json.dumps({"choices": [{"message": {"content": f"![img](data:image/png;base64,{encoded})"}}]})
        ])
        limit = 64
        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", limit, create=True):
            image_response = await self.client.post(
                "/v1/images/generations",
                headers=self._auth_headers(),
                json={"model": "gemini-3.1-flash-image", "prompt": "x" * 256},
            )
            official_response = await self.client.post(
                "/v1/chat/completions",
                headers=self._auth_headers(),
                json={
                    "model": "synthetic-unsupported-model",
                    "messages": [{"role": "user", "content": "x" * 256}],
                },
            )
        self.assertEqual(200, image_response.status_code)
        self.assertNotEqual(413, official_response.status_code)
        self.assertNotEqual(
            "media_too_large",
            official_response.json().get("error", {}).get("code"),
        )


if __name__ == "__main__":
    unittest.main()
