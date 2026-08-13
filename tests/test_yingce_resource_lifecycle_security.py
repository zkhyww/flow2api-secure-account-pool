import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.formparsers import MultiPartParser

from src.api import yingce_adapter
from src.core.config import config
from src.main import app, generation_handler
from src.services.compat_video_tasks import CompatVideoTaskRegistry
from src.services.file_cache import FileCache


class _StreamingResponse:
    status_code = 200
    headers = {}

    @property
    def content(self):
        raise AssertionError("streaming download must not read buffered response content")


class _StreamingSession:
    chunks = []
    get_kwargs = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        type(self).get_kwargs.append(kwargs)
        callback = kwargs.get("content_callback")
        if callback is not None:
            for chunk in type(self).chunks:
                callback(chunk)
        return _StreamingResponse()


def _build_multipart_body(boundary, *, fields, files):
    chunks = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    for name, filename, content_type, payload in files:
        chunks.extend([
            f"--{boundary}\r\n".encode("ascii"),
            (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
             f"Content-Type: {content_type}\r\n\r\n").encode("ascii"),
            payload,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks)


class YingceResourceLifecycleSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        yingce_adapter._background_video_tasks.clear()
        yingce_adapter._background_video_tasks_by_id.clear()
        self.original_api_key = config.api_key
        config.api_key = "yingce-synthetic-api-key"
        self.original_video_tasks = yingce_adapter.video_tasks
        yingce_adapter.video_tasks = CompatVideoTaskRegistry()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        yingce_adapter._background_video_tasks.clear()
        yingce_adapter._background_video_tasks_by_id.clear()
        yingce_adapter.video_tasks = self.original_video_tasks
        config.api_key = self.original_api_key
        await self.client.aclose()

    def _auth_headers(self):
        return {"Authorization": "Bearer yingce-synthetic-api-key"}

    def _capture_generation(self, chunks):
        calls = []
        original = generation_handler.handle_generation

        async def fake_handle_generation(**kwargs):
            calls.append(kwargs)
            for chunk in chunks:
                yield chunk

        generation_handler.handle_generation = fake_handle_generation
        self.addCleanup(setattr, generation_handler, "handle_generation", original)
        return calls

    async def test_multipart_content_length_limit_rejects_before_parser_and_endpoint(self):
        async def forbid_parse(*args, **kwargs):
            self.fail("multipart parser executed before total request limit")

        limit = 256
        request = self.client.build_request(
            "POST",
            "/v1/images/edits",
            headers=self._auth_headers(),
            data={"model": "gemini-3.1-flash-image", "prompt": "synthetic"},
            files={"image": ("reference.png", b"small-reference", "image/png")},
        )
        self.assertGreater(int(request.headers["Content-Length"]), limit)
        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", limit, create=True):
            with patch.object(yingce_adapter, "MAX_UPLOAD_BYTES", 4096):
                with patch.object(MultiPartParser, "parse", forbid_parse):
                    response = await self.client.send(request)
        self.assertEqual(413, response.status_code)
        self.assertEqual("media_too_large", response.json()["error"]["code"])

    async def test_chunked_multipart_limit_stops_before_parser_without_content_length(self):
        async def forbid_parse(*args, **kwargs):
            self.fail("multipart parser executed before streamed total request limit")

        limit = 256
        boundary = "synthetic-boundary"
        body = _build_multipart_body(
            boundary,
            fields={"model": "gemini-3.1-flash-image", "prompt": "synthetic"},
            files=[("image", "reference.png", "image/png", b"r" * 128)],
        )
        chunks = [body[:128], body[128:288], body[288:]]
        sent_count = 0

        async def stream_body():
            nonlocal sent_count
            for chunk in chunks:
                sent_count += 1
                yield chunk

        request = self.client.build_request(
            "POST",
            "/v1/images/edits",
            headers={**self._auth_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"},
            content=stream_body(),
        )
        self.assertNotIn("Content-Length", request.headers)
        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", limit, create=True):
            with patch.object(MultiPartParser, "parse", forbid_parse):
                response = await self.client.send(request)
        self.assertEqual(413, response.status_code)
        self.assertEqual("media_too_large", response.json()["error"]["code"])
        self.assertEqual(2, sent_count)
        self.assertNotIn("synthetic-boundary", response.text)
        self.assertNotIn("reference.png", response.text)

    async def test_forged_low_content_length_still_enforces_actual_received_bytes(self):
        async def forbid_parse(*args, **kwargs):
            self.fail("multipart parser executed before actual received-byte limit")

        limit = 256
        boundary = "forged-length-boundary"
        body = _build_multipart_body(
            boundary,
            fields={"model": "gemini-3.1-flash-image", "prompt": "synthetic"},
            files=[("image", "reference.png", "image/png", b"r" * 256)],
        )
        chunks = [body[:128], body[128:288], body[288:]]
        sent_count = 0

        async def stream_body():
            nonlocal sent_count
            for chunk in chunks:
                sent_count += 1
                yield chunk

        request = self.client.build_request(
            "POST",
            "/v1/images/edits",
            headers={
                **self._auth_headers(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": "64",
            },
            content=stream_body(),
        )
        self.assertEqual("64", request.headers["Content-Length"])
        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", limit, create=True):
            with patch.object(MultiPartParser, "parse", forbid_parse):
                response = await self.client.send(request)

        self.assertEqual(413, response.status_code)
        self.assertEqual("media_too_large", response.json()["error"]["code"])
        self.assertEqual(2, sent_count)

    async def test_multiple_reference_images_are_limited_by_total_multipart_request_size(self):
        fields = {"model": "gemini-3.1-flash-image", "prompt": "synthetic"}
        one_file = self.client.build_request(
            "POST",
            "/v1/images/edits",
            headers=self._auth_headers(),
            data=fields,
            files=[("image", ("reference-1.png", b"a" * 64, "image/png"))],
        )
        two_files = self.client.build_request(
            "POST",
            "/v1/images/edits",
            headers=self._auth_headers(),
            data=fields,
            files=[
                ("image", ("reference-1.png", b"a" * 64, "image/png")),
                ("image", ("reference-2.png", b"b" * 64, "image/png")),
            ],
        )
        one_size = int(one_file.headers["Content-Length"])
        two_size = int(two_files.headers["Content-Length"])
        self.assertLess(one_size, two_size)
        limit = (one_size + two_size) // 2

        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", limit, create=True):
            with patch.object(yingce_adapter, "MAX_UPLOAD_BYTES", 4096):
                response = await self.client.send(two_files)

        self.assertEqual(413, response.status_code)
        self.assertEqual("media_too_large", response.json()["error"]["code"])

    async def test_total_multipart_limit_only_applies_to_yingce_upload_routes(self):
        oversized_header = {**self._auth_headers(), "Content-Length": "4096"}
        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", 8, create=True):
            image_generation = await self.client.post(
                "/v1/images/generations",
                headers=oversized_header,
                json={},
            )
            official_chat = await self.client.post(
                "/v1/chat/completions",
                headers=oversized_header,
                json={},
            )

        self.assertNotEqual(413, image_generation.status_code)
        self.assertNotEqual(413, official_chat.status_code)
        self.assertNotEqual("media_too_large", image_generation.json().get("error", {}).get("code"))
        self.assertNotEqual("media_too_large", official_chat.json().get("error", {}).get("code"))

    async def test_total_multipart_limit_error_does_not_leak_body_or_url(self):
        secret = "synthetic-secret-body-marker"
        request = self.client.build_request(
            "POST",
            f"/v1/videos?source=https://secret.invalid/{secret}",
            headers=self._auth_headers(),
            data={"model": "omni", "prompt": secret},
            files={"input_reference": (f"{secret}.png", b"r" * 64, "image/png")},
        )
        with patch.object(yingce_adapter, "MAX_MULTIPART_REQUEST_BYTES", 16, create=True):
            response = await self.client.send(request)

        self.assertEqual(413, response.status_code)
        self.assertEqual("media_too_large", response.json()["error"]["code"])
        self.assertNotIn(secret, response.text)
        self.assertNotIn("secret.invalid", response.text)

    async def test_oversized_reference_upload_is_rejected_before_generation(self):
        encoded = base64.b64encode(b"synthetic-output").decode("ascii")
        completion = json.dumps(
            {"choices": [{"message": {"content": f"![img](data:image/png;base64,{encoded})"}}]}
        )
        calls = self._capture_generation([completion])

        with patch.object(yingce_adapter, "MAX_UPLOAD_BYTES", 8, create=True):
            response = await self.client.post(
                "/v1/images/edits",
                headers=self._auth_headers(),
                data={"model": "gemini-3.1-flash-image", "prompt": "synthetic"},
                files={"image": ("reference.png", b"0123456789", "image/png")},
            )

        self.assertEqual(413, response.status_code)
        self.assertEqual("media_too_large", response.json()["error"]["code"])
        self.assertEqual([], calls)

    async def test_oversized_inline_base64_is_rejected_without_returning_payload(self):
        encoded = base64.b64encode(b"0123456789").decode("ascii")
        completion = json.dumps(
            {"choices": [{"message": {"content": f"![img](data:image/png;base64,{encoded})"}}]}
        )
        self._capture_generation([completion])

        with patch.object(yingce_adapter, "MAX_INLINE_MEDIA_BYTES", 8, create=True):
            response = await self.client.post(
                "/v1/images/generations",
                headers=self._auth_headers(),
                json={"model": "gemini-3.1-flash-image", "prompt": "synthetic"},
            )

        self.assertEqual(502, response.status_code)
        self.assertEqual("media_too_large", response.json()["error"]["code"])
        self.assertNotIn(encoded, response.text)

    async def test_remote_download_stops_at_limit_and_leaves_no_partial_file(self):
        public_dns = [(2, 1, 6, "", ("142.250.72.97", 443))]
        _StreamingSession.chunks = [b"1234", b"5678", b"9"]
        _StreamingSession.get_kwargs = []
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", return_value=public_dns):
                with patch("src.services.file_cache.AsyncSession", _StreamingSession):
                    with patch.object(cache, "max_remote_media_bytes", 8, create=True):
                        with self.assertRaisesRegex(Exception, "remote_media_too_large"):
                            await cache.download_and_cache(
                                "https://flow-content.google/synthetic.mp4",
                                "video",
                                log_source_url=False,
                            )
            self.assertEqual([], list(Path(temp_dir).glob("*.part")))
            self.assertEqual([], list(Path(temp_dir).glob("*.mp4")))

        self.assertEqual(1, len(_StreamingSession.get_kwargs))
        self.assertIn("content_callback", _StreamingSession.get_kwargs[0])
        self.assertTrue(callable(_StreamingSession.get_kwargs[0]["content_callback"]))

    async def test_secure_remote_download_never_disables_tls_verification(self):
        public_dns = [(2, 1, 6, "", ("142.250.72.97", 443))]
        _StreamingSession.chunks = [b"synthetic-media"]
        _StreamingSession.get_kwargs = []
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", return_value=public_dns):
                with patch("src.services.file_cache.AsyncSession", _StreamingSession):
                    await cache.download_and_cache(
                        "https://flow-content.google/synthetic.mp4",
                        "video",
                        log_source_url=False,
                    )

        self.assertEqual(1, len(_StreamingSession.get_kwargs))
        self.assertIsNot(False, _StreamingSession.get_kwargs[0].get("verify", True))
        self.assertTrue(_StreamingSession.get_kwargs[0].get("verify", True))

    async def test_video_poll_url_is_relative_and_ignores_untrusted_host(self):
        task = await yingce_adapter.video_tasks.create(model="omni", size=None, seconds=10)
        await yingce_adapter.video_tasks.update(task.id, status="completed", progress=100)

        response = await self.client.get(
            f"/v1/videos/{task.id}",
            headers={**self._auth_headers(), "Host": "attacker.invalid"},
        )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual(f"/v1/videos/{task.id}/content", payload["url"])
        self.assertNotIn("attacker.invalid", response.text)

    async def test_background_task_exception_is_consumed_and_marks_task_failed(self):
        async def explode_before_try(*args, **kwargs):
            raise RuntimeError("synthetic-private-background-failure")

        original = yingce_adapter._run_video_task
        yingce_adapter._run_video_task = explode_before_try
        self.addCleanup(setattr, yingce_adapter, "_run_video_task", original)

        created = await self.client.post(
            "/v1/videos",
            headers=self._auth_headers(),
            data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
        )
        self.assertEqual(200, created.status_code)
        task_id = created.json()["id"]
        await asyncio.sleep(0)

        polled = await self.client.get(f"/v1/videos/{task_id}", headers=self._auth_headers())
        self.assertEqual(200, polled.status_code)
        self.assertEqual("failed", polled.json()["status"])
        self.assertEqual("generation_failed", polled.json()["error"]["code"])
        self.assertNotIn("synthetic-private-background-failure", polled.text)
        self.assertEqual(set(), yingce_adapter._background_video_tasks)

    async def test_shutdown_helper_cancels_and_awaits_background_video_tasks(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending_work():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(pending_work())
        yingce_adapter._background_video_tasks.add(task)
        yingce_adapter._background_video_tasks_by_id["synthetic-shutdown-task"] = task
        await started.wait()

        shutdown = getattr(yingce_adapter, "shutdown_background_video_tasks", None)
        self.assertIsNotNone(shutdown)
        await asyncio.wait_for(shutdown(timeout_seconds=0.2), timeout=1.0)

        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.done())
        self.assertEqual(set(), yingce_adapter._background_video_tasks)
        self.assertEqual({}, yingce_adapter._background_video_tasks_by_id)

    async def test_active_ttl_cancels_upstream_and_cannot_be_overwritten_by_completion(self):
        now = [1000.0]
        yingce_adapter.video_tasks = CompatVideoTaskRegistry(
            ttl_seconds=1,
            capacity=1,
            clock=lambda: now[0],
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocking_generation(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "<video src='/tmp/should-never-complete.mp4'></video>"
                            }
                        }
                    ]
                }
            )

        original = generation_handler.handle_generation
        generation_handler.handle_generation = blocking_generation
        self.addCleanup(setattr, generation_handler, "handle_generation", original)

        created = await self.client.post(
            "/v1/videos",
            headers=self._auth_headers(),
            data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
        )
        self.assertEqual(200, created.status_code)
        task_id = created.json()["id"]
        await asyncio.wait_for(started.wait(), timeout=1.0)

        now[0] = 1002.0
        expired = await yingce_adapter.video_tasks.get(task_id)
        self.assertIsNotNone(expired)
        self.assertEqual("failed", expired.status)
        self.assertEqual("task_timeout", expired.error_code)

        await asyncio.wait_for(cancelled.wait(), timeout=0.2)
        await asyncio.sleep(0)

        expired = await yingce_adapter.video_tasks.get(task_id)
        self.assertIsNotNone(expired)
        self.assertEqual("failed", expired.status)
        self.assertEqual("task_timeout", expired.error_code)
        self.assertIsNone(expired.filename)

        replacement = await yingce_adapter.video_tasks.create(
            model="omni",
            size=None,
            seconds=10,
        )
        self.assertNotEqual(task_id, replacement.id)
        self.assertIsNone(await yingce_adapter.video_tasks.get(task_id))
        self.assertEqual(set(), yingce_adapter._background_video_tasks)
        task_map = getattr(yingce_adapter, "_background_video_tasks_by_id", None)
        self.assertIsNotNone(task_map)
        self.assertEqual({}, task_map)


if __name__ == "__main__":
    unittest.main()
