import asyncio
import base64
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import httpx

from src.api import yingce_adapter
from src.core.config import config
from src.services.flow_client import FlowApiError
from src.main import app, generation_handler
from src.services.compat_video_tasks import CompatVideoTaskRegistry


class _SyntheticMediaProxyManager:
    async def get_media_proxy_url(self):
        return "http://synthetic-proxy.invalid:8080"


class _SyntheticUnsupportedMediaProxyManager:
    async def get_media_proxy_url(self):
        return "https://synthetic-proxy.invalid:8443"


class _SyntheticRemoteResponse:
    status_code = 200
    headers = {}


class _SyntheticRemoteSession:
    init_kwargs = []
    get_calls = []
    payload = b"synthetic-remote-video-bytes"

    def __init__(self, *args, **kwargs):
        type(self).init_kwargs.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        type(self).get_calls.append((url, kwargs))
        callback = kwargs.get("content_callback")
        if callback is not None:
            callback(type(self).payload)
        return _SyntheticRemoteResponse()


class YingceAdapterAuthContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_api_key = config.api_key
        config.api_key = "yingce-synthetic-api-key"
        self.original_video_tasks = yingce_adapter.video_tasks
        yingce_adapter.video_tasks = CompatVideoTaskRegistry()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        pending = list(yingce_adapter._background_video_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
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

    async def _poll_video_terminal(self, task_id):
        for _ in range(20):
            response = await self.client.get(
                f"/v1/videos/{task_id}", headers=self._auth_headers()
            )
            payload = response.json()
            if payload["status"] in {"completed", "failed"}:
                return payload
            await asyncio.sleep(0)
        return payload

    def _video_reference_files(self, count):
        return [
            (
                "input_reference[]",
                (
                    f"reference-{index}.png",
                    f"synthetic-reference-{index}".encode("ascii"),
                    "image/png",
                ),
            )
            for index in range(count)
        ]

    def test_video_public_capability_ids_are_exact_and_never_disambiguated_by_image_count(self):
        explicit_cases = (
            ("omni-flash", 0, "omni", "omni-flash"),
            ("omni-flash-references", 1, "omni", "omni-flash-references"),
            (
                "veo-3.1-fast-first-frame",
                1,
                "veo_3_1_i2v_s_fast_landscape_8s_fl",
                "veo-3.1-fast-first-frame",
            ),
            (
                "veo-3.1-fast-first-last",
                2,
                "veo_3_1_i2v_s_fast_landscape_8s_fl",
                "veo-3.1-fast-first-last",
            ),
        )
        for model, reference_count, expected_model, expected_capability in explicit_cases:
            with self.subTest(model=model, reference_count=reference_count):
                resolved_model, duration, capability = yingce_adapter._resolve_video_model(
                    model,
                    "16:9",
                    8,
                    reference_count,
                )
                self.assertEqual(expected_model, resolved_model)
                self.assertEqual(8, duration)
                self.assertEqual(expected_capability, capability["capability_id"])

        rejected_cases = (
            ("omni-flash", 1),
            ("omni-flash-references", 0),
            ("veo-3.1-fast-first-frame", 2),
            ("veo-3.1-fast-first-last", 1),
        )
        for model, reference_count in rejected_cases:
            with self.subTest(model=model, rejected_reference_count=reference_count):
                with self.assertRaises(yingce_adapter.VideoParameterError):
                    yingce_adapter._resolve_video_model(
                        model,
                        "16:9",
                        8,
                        reference_count,
                    )

    def test_video_legacy_internal_ids_disambiguate_only_by_reference_count(self):
        legacy_cases = (
            ("omni", 0, "omni", "omni-flash"),
            ("omni", 1, "omni", "omni-flash-references"),
            ("omni", 3, "omni", "omni-flash-references"),
            (
                "veo_3_1_i2v_s_fast_landscape_8s_fl",
                1,
                "veo_3_1_i2v_s_fast_landscape_8s_fl",
                "veo-3.1-fast-first-frame",
            ),
            (
                "veo_3_1_i2v_s_fast_landscape_8s_fl",
                2,
                "veo_3_1_i2v_s_fast_landscape_8s_fl",
                "veo-3.1-fast-first-last",
            ),
            (
                "veo_3_1_i2v_s_landscape_8s",
                1,
                "veo_3_1_i2v_s_landscape_8s",
                "veo-3.1-quality-first-frame",
            ),
            (
                "veo_3_1_i2v_s_landscape_8s",
                2,
                "veo_3_1_i2v_s_landscape_8s",
                "veo-3.1-quality-first-last",
            ),
        )
        for model, reference_count, expected_model, expected_capability in legacy_cases:
            with self.subTest(model=model, reference_count=reference_count):
                resolved_model, duration, capability = yingce_adapter._resolve_video_model(
                    model,
                    None,
                    None,
                    reference_count,
                )
                self.assertEqual(expected_model, resolved_model)
                self.assertEqual(8, duration)
                self.assertEqual(expected_capability, capability["capability_id"])

    async def test_all_yingce_routes_reuse_existing_api_key_auth(self):
        requests = (
            ("POST", "/v1/images/generations", {"json": {"model": "gemini-3.1-flash-image", "prompt": "synthetic"}}),
            ("POST", "/v1/images/edits", {"data": {"model": "gemini-3.1-flash-image", "prompt": "synthetic"}}),
            ("POST", "/v1/videos", {"data": {"model": "omni", "prompt": "synthetic"}}),
            ("GET", "/v1/videos/synthetic-task", {}),
            ("GET", "/v1/videos/synthetic-task/content", {}),
        )

        for method, path, kwargs in requests:
            with self.subTest(method=method, path=path):
                response = await self.client.request(method, path, **kwargs)
                self.assertEqual(401, response.status_code)
                self.assertEqual("Invalid API key", response.json()["detail"])

    async def test_image_generations_maps_openai_fields_and_prefers_b64_json(self):
        encoded = "c3ludGhldGljLWltYWdlLWJ5dGVz"
        completion = json.dumps(
            {
                "id": "chatcmpl-synthetic",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"![Generated Image](data:image/png;base64,{encoded})",
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )
        calls = self._capture_generation([completion])

        response = await self.client.post(
            "/v1/images/generations",
            headers=self._auth_headers(),
            json={
                "model": "gemini-3.1-flash-image",
                "prompt": "synthetic image prompt",
                "n": 1,
                "size": "1024x1024",
                "quality": "high",
            },
        )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertIsInstance(payload["created"], int)
        self.assertEqual([{"b64_json": encoded}], payload["data"])
        self.assertEqual(1, len(calls))
        self.assertEqual("gemini-3.1-flash-image-square-4k", calls[0]["model"])
        self.assertEqual("synthetic image prompt", calls[0]["prompt"])
        self.assertIsNone(calls[0]["images"])
        self.assertFalse(calls[0]["stream"])
        self.assertEqual("http://testserver", calls[0]["base_url_override"])

    async def test_image_generation_allowlisted_https_url_does_not_download_with_proxy_configured(self):
        remote_uri = "https://flow-content.google/synthetic-image.png"
        completion = json.dumps(
            {"choices": [{"message": {"content": f"![img]({remote_uri})"}}]}
        )
        self._capture_generation([completion])

        original_proxy_manager = generation_handler.file_cache.proxy_manager
        original_download = generation_handler.file_cache.download_and_cache
        generation_handler.file_cache.proxy_manager = _SyntheticMediaProxyManager()
        download_calls = []

        async def forbid_download(*args, **kwargs):
            download_calls.append((args, kwargs))
            raise AssertionError("allowlisted image URL must not be downloaded server-side")

        generation_handler.file_cache.download_and_cache = forbid_download
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "proxy_manager",
            original_proxy_manager,
        )
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "download_and_cache",
            original_download,
        )

        response = await self.client.post(
            "/v1/images/generations",
            headers=self._auth_headers(),
            json={
                "model": "gemini-3.1-flash-image",
                "prompt": "synthetic",
                "size": "1792x1024",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual([{"url": remote_uri}], response.json()["data"])
        self.assertEqual([], download_calls)

    async def test_image_generation_rejects_untrusted_remote_urls_without_reflection(self):
        candidates = (
            "http://flow-content.google/synthetic-image.png",
            "https://user@flow-content.google/synthetic-image.png",
            "https://flow-content.google:444/synthetic-image.png",
            "https://untrusted.invalid/synthetic-image.png",
        )
        original_download = generation_handler.file_cache.download_and_cache
        download_calls = []

        async def forbid_download(*args, **kwargs):
            download_calls.append((args, kwargs))
            raise AssertionError("rejected image URL must not be downloaded")

        generation_handler.file_cache.download_and_cache = forbid_download
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "download_and_cache",
            original_download,
        )

        for remote_uri in candidates:
            with self.subTest(kind=remote_uri.split(":", 1)[0]):
                completion = json.dumps(
                    {"choices": [{"message": {"content": f"![img]({remote_uri})"}}]}
                )
                self._capture_generation([completion])
                response = await self.client.post(
                    "/v1/images/generations",
                    headers=self._auth_headers(),
                    json={"model": "gemini-3.1-flash-image", "prompt": "synthetic"},
                )
                self.assertEqual(502, response.status_code)
                self.assertEqual("media_empty", response.json()["error"]["code"])
                self.assertNotIn(remote_uri, response.text)

        self.assertEqual([], download_calls)

    async def test_image_generation_malformed_provider_error_stays_stable(self):
        raw_detail = "synthetic-private-image-provider-detail"
        completion = json.dumps(
            {
                "error": {
                    "message": raw_detail,
                    "code": "raw_image_provider_fixture",
                    "status_code": "not-an-integer",
                }
            }
        )
        self._capture_generation([completion])

        response = await self.client.post(
            "/v1/images/generations",
            headers=self._auth_headers(),
            json={
                "model": "gemini-3.1-flash-image",
                "prompt": "synthetic image failure prompt",
            },
        )

        self.assertEqual(502, response.status_code)
        self.assertEqual("generation_failed", response.json()["error"]["code"])
        self.assertNotIn(raw_detail, response.text)
        self.assertNotIn("synthetic image failure prompt", response.text)

    async def test_image_generations_prefers_b64_json_for_local_cached_media(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        original_cache_dir = generation_handler.file_cache.cache_dir
        generation_handler.file_cache.cache_dir = Path(temp_dir.name)
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "cache_dir",
            original_cache_dir,
        )
        image_bytes = b"synthetic-local-image-bytes"
        filename = "synthetic-image.jpg"
        (Path(temp_dir.name) / filename).write_bytes(image_bytes)
        completion = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": f"![Generated Image](http://testserver/tmp/{filename})"
                        }
                    }
                ]
            }
        )
        self._capture_generation([completion])

        response = await self.client.post(
            "/v1/images/generations",
            headers=self._auth_headers(),
            json={
                "model": "gemini-3.1-flash-image",
                "prompt": "synthetic cached image prompt",
            },
        )

        self.assertEqual(200, response.status_code)
        expected = base64.b64encode(image_bytes).decode("ascii")
        self.assertEqual([{"b64_json": expected}], response.json()["data"])

    async def test_image_edits_passes_reference_image_to_existing_handler(self):
        encoded = "ZWRpdGVkLXN5bnRoZXRpYy1pbWFnZQ=="
        completion = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": f"![Generated Image](data:image/png;base64,{encoded})"
                        }
                    }
                ]
            }
        )
        calls = self._capture_generation([completion])
        reference_bytes = b"\x89PNG\r\n\x1a\nsynthetic-reference"

        response = await self.client.post(
            "/v1/images/edits",
            headers=self._auth_headers(),
            data={
                "model": "gemini-3.1-flash-image",
                "prompt": "synthetic edit prompt",
                "size": "1024x1024",
                "quality": "standard",
            },
            files={"image": ("reference.png", reference_bytes, "image/png")},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual([{"b64_json": encoded}], response.json()["data"])
        self.assertEqual(1, len(calls))
        self.assertEqual([reference_bytes], calls[0]["images"])
        self.assertEqual("gemini-3.1-flash-image-square", calls[0]["model"])
        self.assertEqual("synthetic edit prompt", calls[0]["prompt"])

    async def test_image_edits_rejects_mask_without_submitting_generation(self):
        calls = self._capture_generation([])

        response = await self.client.post(
            "/v1/images/edits",
            headers=self._auth_headers(),
            data={
                "model": "gemini-3.1-flash-image",
                "prompt": "synthetic masked edit prompt",
            },
            files={
                "image": ("reference.png", b"synthetic-image", "image/png"),
                "mask": ("mask.png", b"synthetic-mask", "image/png"),
            },
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual("mask_not_supported", response.json()["error"]["code"])
        self.assertEqual([], calls)

    async def test_video_720p_native_and_blank_keep_the_catalog_model_without_upsample(self):
        completion = json.dumps(
            {
                "error": {
                    "message": "synthetic native resolution fixture",
                    "code": "upstream_error",
                    "status_code": 502,
                }
            }
        )
        calls = []

        async def fake_handle_generation(**kwargs):
            calls.append(kwargs)
            yield completion

        with patch.object(generation_handler, "handle_generation", fake_handle_generation):
            for resolution_name in (None, "", "native", "720P", "720p"):
                with self.subTest(resolution_name=resolution_name):
                    data = {
                        "model": "veo-3.1-quality",
                        "prompt": "synthetic native resolution prompt",
                        "seconds": "8",
                        "size": "1792x1024",
                    }
                    if resolution_name is not None:
                        data["resolution_name"] = resolution_name
                    response = await self.client.post(
                        "/v1/videos",
                        headers=self._auth_headers(),
                        data=data,
                    )
                    self.assertEqual(200, response.status_code)
                    await self._poll_video_terminal(response.json()["id"])
                    self.assertEqual(
                        "veo_3_1_t2v_landscape_8s",
                        calls[-1]["model"],
                    )

        self.assertEqual(5, len(calls))

    async def test_video_nativep_alias_keeps_veo_lite_native_model(self):
        completion = json.dumps(
            {"error": {"code": "upstream_error", "status_code": 502}}
        )
        calls = []

        async def fake_handle_generation(**kwargs):
            calls.append(kwargs)
            yield completion

        with patch.object(generation_handler, "handle_generation", fake_handle_generation):
            for resolution_name in ("nativeP", "nativep"):
                with self.subTest(resolution_name=resolution_name):
                    response = await self.client.post(
                        "/v1/videos",
                        headers=self._auth_headers(),
                        data={
                            "model": "veo-3.1-lite",
                            "prompt": "synthetic native alias prompt",
                            "seconds": "8",
                            "size": "16:9",
                            "resolution_name": resolution_name,
                        },
                    )
                    self.assertEqual(200, response.status_code)
                    await self._poll_video_terminal(response.json()["id"])
                    self.assertEqual(
                        "veo_3_1_t2v_lite_landscape_8s",
                        calls[-1]["model"],
                    )

        self.assertEqual(2, len(calls))

    async def test_video_jutian_pixel_sizes_map_to_catalog_aspect_ratios(self):
        completion = json.dumps(
            {"error": {"code": "upstream_error", "status_code": 502}}
        )
        calls = []

        async def fake_handle_generation(**kwargs):
            calls.append(kwargs)
            yield completion

        cases = (
            ("1280x720", "veo_3_1_t2v_fast_landscape_8s"),
            ("720x1280", "veo_3_1_t2v_fast_portrait_8s"),
        )
        with patch.object(generation_handler, "handle_generation", fake_handle_generation):
            for size, expected_model in cases:
                with self.subTest(size=size):
                    response = await self.client.post(
                        "/v1/videos",
                        headers=self._auth_headers(),
                        data={
                            "model": "veo-3.1-fast",
                            "prompt": "synthetic Jutian pixel size prompt",
                            "seconds": "8",
                            "size": size,
                            "resolution_name": "native",
                        },
                    )
                    self.assertEqual(200, response.status_code)
                    await self._poll_video_terminal(response.json()["id"])
                    self.assertEqual(expected_model, calls[-1]["model"])

        self.assertEqual(2, len(calls))

    async def test_video_public_text_modes_map_to_exact_existing_models(self):
        completion = json.dumps(
            {"error": {"code": "upstream_error", "status_code": 502}}
        )
        calls = []

        async def fake_handle_generation(**kwargs):
            calls.append(kwargs)
            yield completion

        cases = (
            ("omni-flash", 8, "16:9", "omni"),
            ("omni-flash", 10, "9:16", "omni_portrait_10s"),
            ("veo-3.1-lite", 8, "16:9", "veo_3_1_t2v_lite_landscape_8s"),
            ("veo-3.1-fast", 8, "9:16", "veo_3_1_t2v_fast_portrait_8s"),
            ("veo-3.1-quality", 8, "16:9", "veo_3_1_t2v_landscape_8s"),
        )
        with patch.object(generation_handler, "handle_generation", fake_handle_generation):
            for model, seconds, size, expected_model in cases:
                with self.subTest(model=model, seconds=seconds, size=size):
                    response = await self.client.post(
                        "/v1/videos",
                        headers=self._auth_headers(),
                        data={
                            "model": model,
                            "prompt": "synthetic text video prompt",
                            "seconds": str(seconds),
                            "size": size,
                            "resolution_name": "720P",
                        },
                    )
                    self.assertEqual(200, response.status_code)
                    await self._poll_video_terminal(response.json()["id"])
                    self.assertEqual(expected_model, calls[-1]["model"])
                    self.assertIsNone(calls[-1]["images"])

    async def test_video_public_image_modes_map_to_exact_existing_models(self):
        completion = json.dumps(
            {"error": {"code": "upstream_error", "status_code": 502}}
        )
        calls = []

        async def fake_handle_generation(**kwargs):
            calls.append(kwargs)
            yield completion

        cases = (
            ("omni-flash-references", 1, "16:9", "omni"),
            ("omni-flash-references", 3, "9:16", "omni_portrait"),
            ("veo-3.1-lite-first-frame", 1, "16:9", "veo_3_1_i2v_lite_landscape_8s"),
            ("veo-3.1-lite-first-last", 2, "9:16", "veo_3_1_interpolation_lite_portrait_8s"),
            ("veo-3.1-fast-first-frame", 1, "16:9", "veo_3_1_i2v_s_fast_landscape_8s_fl"),
            ("veo-3.1-fast-first-last", 2, "9:16", "veo_3_1_i2v_s_fast_portrait_8s_fl"),
            ("veo-3.1-fast-references", 1, "16:9", "veo_3_1_r2v_fast_landscape"),
            ("veo-3.1-fast-references", 3, "9:16", "veo_3_1_r2v_fast_portrait"),
            ("veo-3.1-quality-first-frame", 1, "16:9", "veo_3_1_i2v_s_landscape_8s"),
            ("veo-3.1-quality-first-last", 2, "9:16", "veo_3_1_i2v_s_portrait_8s"),
        )
        with patch.object(generation_handler, "handle_generation", fake_handle_generation):
            for model, image_count, size, expected_model in cases:
                with self.subTest(model=model, image_count=image_count, size=size):
                    response = await self.client.post(
                        "/v1/videos",
                        headers=self._auth_headers(),
                        data={
                            "model": model,
                            "prompt": "synthetic image video prompt",
                            "seconds": "8",
                            "size": size,
                            "resolution_name": "720P",
                        },
                        files=self._video_reference_files(image_count),
                    )
                    self.assertEqual(200, response.status_code)
                    await self._poll_video_terminal(response.json()["id"])
                    self.assertEqual(expected_model, calls[-1]["model"])
                    self.assertEqual(image_count, len(calls[-1]["images"]))

    async def test_video_explicit_modes_reject_wrong_image_counts_before_task_creation(self):
        calls = self._capture_generation([])
        cases = (
            ("omni-flash", 1, 8),
            ("omni-flash-references", 0, 8),
            ("omni-flash-references", 4, 8),
            ("omni-flash-references", 1, 10),
            ("veo-3.1-lite-first-frame", 0, 8),
            ("veo-3.1-lite-first-frame", 2, 8),
            ("veo-3.1-lite-first-last", 1, 8),
            ("veo-3.1-lite-first-last", 3, 8),
            ("veo-3.1-fast-first-frame", 0, 8),
            ("veo-3.1-fast-first-frame", 2, 8),
            ("veo-3.1-fast-first-last", 1, 8),
            ("veo-3.1-fast-first-last", 3, 8),
            ("veo-3.1-fast-references", 0, 8),
            ("veo-3.1-fast-references", 4, 8),
            ("veo-3.1-quality-first-frame", 0, 8),
            ("veo-3.1-quality-first-frame", 2, 8),
            ("veo-3.1-quality-first-last", 1, 8),
            ("veo-3.1-quality-first-last", 3, 8),
        )
        for model, image_count, seconds in cases:
            with self.subTest(model=model, image_count=image_count, seconds=seconds):
                task_count = len(yingce_adapter.video_tasks._tasks)
                response = await self.client.post(
                    "/v1/videos",
                    headers=self._auth_headers(),
                    data={
                        "model": model,
                        "prompt": "synthetic private prompt marker",
                        "seconds": str(seconds),
                        "size": "16:9",
                        "resolution_name": "720P",
                    },
                    files=self._video_reference_files(image_count) or None,
                )
                self.assertEqual(400, response.status_code)
                payload = response.json()
                self.assertEqual("unsupported_video_parameters", payload["error"]["code"])
                self.assertIn("allowed", payload["error"])
                self.assertEqual(task_count, len(yingce_adapter.video_tasks._tasks))
                self.assertNotIn("synthetic private prompt marker", response.text)
                self.assertNotIn("synthetic-reference", response.text)
        self.assertEqual([], calls)

    async def test_video_unverified_resolutions_fail_closed_without_upsample_or_task(self):
        calls = self._capture_generation([])
        cases = (
            ("omni-flash", "1080P"),
            ("veo-3.1-lite", "1080p"),
            ("veo-3.1-fast", "4K"),
            ("veo-3.1-quality", "2160P"),
            ("veo-3.1-quality", "480P"),
        )
        for model, resolution_name in cases:
            with self.subTest(model=model, resolution_name=resolution_name):
                task_count = len(yingce_adapter.video_tasks._tasks)
                response = await self.client.post(
                    "/v1/videos",
                    headers=self._auth_headers(),
                    data={
                        "model": model,
                        "prompt": "synthetic resolution rejection prompt",
                        "seconds": "8",
                        "size": "16:9",
                        "resolution_name": resolution_name,
                    },
                )
                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    "unsupported_video_parameters",
                    response.json()["error"]["code"],
                )
                self.assertEqual(task_count, len(yingce_adapter.video_tasks._tasks))
        self.assertEqual([], calls)

    async def test_video_invalid_aspect_and_duration_return_safe_allowed_metadata(self):
        calls = self._capture_generation([])
        cases = (
            {"model": "veo-3.1-fast", "seconds": "6", "size": "16:9"},
            {"model": "veo-3.1-fast", "seconds": "8", "size": "1024x1024"},
        )
        for request_data in cases:
            with self.subTest(request_data=request_data):
                task_count = len(yingce_adapter.video_tasks._tasks)
                response = await self.client.post(
                    "/v1/videos",
                    headers=self._auth_headers(),
                    data={
                        **request_data,
                        "prompt": "synthetic safe error prompt marker",
                        "resolution_name": "720P",
                    },
                )
                self.assertEqual(400, response.status_code)
                error = response.json()["error"]
                self.assertEqual("unsupported_video_parameters", error["code"])
                self.assertEqual("veo-3.1-fast", error["allowed"]["capability_id"])
                self.assertEqual(["16:9", "9:16"], error["allowed"]["aspect_ratio"])
                self.assertEqual([8], error["allowed"]["duration_seconds"])
                self.assertEqual(
                    ["native", "nativeP", "720P"],
                    error["allowed"]["resolution"],
                )
                self.assertEqual({"min": 0, "max": 0}, error["allowed"]["images"])
                self.assertNotIn("synthetic safe error prompt marker", response.text)
                self.assertEqual(task_count, len(yingce_adapter.video_tasks._tasks))
        self.assertEqual([], calls)

    async def test_video_quality_references_alias_is_not_publicly_resolvable(self):
        calls = self._capture_generation([])
        task_count = len(yingce_adapter.video_tasks._tasks)
        response = await self.client.post(
            "/v1/videos",
            headers=self._auth_headers(),
            data={
                "model": "veo-3.1-quality-references",
                "prompt": "synthetic quality refs prompt",
                "seconds": "8",
                "size": "16:9",
                "resolution_name": "720P",
            },
            files=self._video_reference_files(1),
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual("unsupported_model", response.json()["error"]["code"])
        self.assertEqual(task_count, len(yingce_adapter.video_tasks._tasks))
        self.assertEqual([], calls)

    def test_video_marker_parser_accepts_generation_handler_completion_format(self):
        completion = generation_handler._create_completion_response(
            "data:video/mp4;base64,c3ludGhldGlj", media_type="video"
        )
        content = yingce_adapter._extract_completion_content(json.loads(completion))
        marker = yingce_adapter.VIDEO_HTML_RE.search(content)

        self.assertIsNotNone(marker)
        self.assertIsNotNone(yingce_adapter.DATA_VIDEO_RE.match(marker.group(1).strip()))

    async def test_video_create_poll_completed_and_content_proxy(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        original_cache_dir = generation_handler.file_cache.cache_dir
        generation_handler.file_cache.cache_dir = Path(temp_dir.name)
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "cache_dir",
            original_cache_dir,
        )
        filename = "synthetic-video.mp4"
        video_bytes = b"synthetic-video-bytes"
        (Path(temp_dir.name) / filename).write_bytes(video_bytes)
        completion = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```html\n"
                                f"<video src='http://testserver/tmp/{filename}' controls></video>\n"
                                "```"
                            )
                        }
                    }
                ]
            }
        )
        calls = self._capture_generation([completion])

        create_response = await self.client.post(
            "/v1/videos",
            headers=self._auth_headers(),
            data={
                "model": "omni",
                "prompt": "synthetic video prompt",
                "seconds": "10",
                "size": "1792x1024",
            },
        )

        self.assertEqual(200, create_response.status_code)
        created = create_response.json()
        task_id = created["id"]
        self.assertTrue(task_id.startswith("video_"))
        self.assertEqual("video", created["object"])
        self.assertIn(created["status"], {"queued", "in_progress", "completed"})
        self.assertEqual("omni", created["model"])
        self.assertEqual(10, created["seconds"])

        polled = None
        for _ in range(20):
            poll_response = await self.client.get(
                f"/v1/videos/{task_id}", headers=self._auth_headers()
            )
            self.assertEqual(200, poll_response.status_code)
            polled = poll_response.json()
            if polled["status"] == "completed":
                break
            await asyncio.sleep(0)

        self.assertIsNotNone(polled)
        self.assertEqual("completed", polled["status"])
        self.assertEqual(100, polled["progress"])
        self.assertTrue(polled["url"].endswith(f"/v1/videos/{task_id}/content"))
        self.assertIsNone(polled["error"])

        content_response = await self.client.get(
            f"/v1/videos/{task_id}/content", headers=self._auth_headers()
        )
        self.assertEqual(200, content_response.status_code)
        self.assertEqual(video_bytes, content_response.content)
        self.assertEqual(1, len(calls))
        self.assertEqual("omni_10s", calls[0]["model"])
        self.assertEqual("synthetic video prompt", calls[0]["prompt"])
        self.assertIsNone(calls[0]["images"])
        self.assertFalse(calls[0]["stream"])

    async def test_video_data_url_is_cached_before_local_content_proxy(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        original_cache_dir = generation_handler.file_cache.cache_dir
        generation_handler.file_cache.cache_dir = Path(temp_dir.name)
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "cache_dir",
            original_cache_dir,
        )
        video_bytes = b"synthetic-inline-video"
        encoded = base64.b64encode(video_bytes).decode("ascii")
        cached_filename = "cached-inline-video.mp4"
        cache_calls = []
        original_cache = generation_handler.file_cache.cache_base64_video

        async def fake_cache_base64_video(value):
            cache_calls.append(value)
            (Path(temp_dir.name) / cached_filename).write_bytes(video_bytes)
            return cached_filename

        generation_handler.file_cache.cache_base64_video = fake_cache_base64_video
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "cache_base64_video",
            original_cache,
        )
        completion = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```html\n"
                                f"<video src='data:video/mp4;base64,{encoded}' controls></video>\n"
                                "```"
                            )
                        }
                    }
                ]
            }
        )
        self._capture_generation([completion])

        created = await self.client.post(
            "/v1/videos",
            headers=self._auth_headers(),
            data={"model": "omni", "prompt": "synthetic inline video prompt", "seconds": "10"},
        )
        self.assertEqual(200, created.status_code)
        task_id = created.json()["id"]

        polled = None
        for _ in range(20):
            response = await self.client.get(
                f"/v1/videos/{task_id}", headers=self._auth_headers()
            )
            polled = response.json()
            if polled["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0)

        self.assertIsNotNone(polled)
        self.assertEqual("completed", polled["status"])
        self.assertEqual([encoded], cache_calls)
        content = await self.client.get(
            f"/v1/videos/{task_id}/content", headers=self._auth_headers()
        )
        self.assertEqual(200, content.status_code)
        self.assertEqual(video_bytes, content.content)

    async def test_video_handler_structured_failures_are_classified_without_leak(self):
        structured_private = RuntimeError("synthetic-private-structured-detail")
        structured_private.error_code = "RECAPTCHA"
        structured_private.status_code = 0
        cases = (
            ("recaptcha", FlowApiError(error_code="RECAPTCHA")),
            ("recaptcha", structured_private),
            ("authentication", FlowApiError(error_code="UNAUTHENTICATED")),
            ("model_access_denied", FlowApiError(error_code="MODEL_ACCESS_DENIED")),
            ("membership_tier", FlowApiError(error_code="MEMBERSHIP_TIER")),
            ("quota_exhausted", FlowApiError(error_code="RESOURCE_EXHAUSTED")),
            ("content_policy", FlowApiError(error_code="CONTENT_POLICY")),
            ("rate_limited", FlowApiError(status_code=429)),
            ("upstream_5xx", FlowApiError(status_code=503)),
            ("upstream_error", FlowApiError(error_code="UPSTREAM_ERROR")),
            ("submission_uncertain", asyncio.TimeoutError("synthetic-private-timeout-detail")),
            ("generation_failed", RuntimeError("synthetic-private-unknown-detail")),
        )

        for expected, error in cases:
            for phase in ("before_yield", "during_iteration"):
                with self.subTest(expected=expected, phase=phase):
                    async def failing_generation(**kwargs):
                        if phase == "during_iteration":
                            yield json.dumps({"choices": []})
                        raise error

                    with patch.object(generation_handler, "handle_generation", failing_generation):
                        created = await self.client.post(
                            "/v1/videos",
                            headers=self._auth_headers(),
                            data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
                        )
                        self.assertEqual(200, created.status_code)
                        polled = await self._poll_video_terminal(created.json()["id"])

                    self.assertEqual("failed", polled["status"])
                    self.assertEqual(expected, polled["error"]["code"])
                    serialized = json.dumps(polled)
                    self.assertNotIn("synthetic-private-timeout-detail", serialized)
                    self.assertNotIn("synthetic-private-unknown-detail", serialized)
                    self.assertNotIn("synthetic-private-structured-detail", serialized)

    async def test_video_payload_error_uses_same_public_failure_classes(self):
        public_codes = (
            "recaptcha",
            "authentication",
            "model_access_denied",
            "membership_tier",
            "quota_exhausted",
            "content_policy",
            "rate_limited",
            "upstream_5xx",
            "upstream_error",
            "submission_uncertain",
        )
        for expected in public_codes:
            with self.subTest(expected=expected):
                completion = json.dumps(
                    {"error": {"code": expected, "message": "synthetic-private-payload-detail"}}
                )

                async def payload_error_generation(**kwargs):
                    yield completion

                with patch.object(
                    generation_handler,
                    "handle_generation",
                    payload_error_generation,
                ):
                    created = await self.client.post(
                        "/v1/videos",
                        headers=self._auth_headers(),
                        data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
                    )
                    self.assertEqual(200, created.status_code)
                    polled = await self._poll_video_terminal(created.json()["id"])

                self.assertEqual("failed", polled["status"])
                self.assertEqual(expected, polled["error"]["code"])
                self.assertNotIn("synthetic-private-payload-detail", json.dumps(polled))

    async def test_video_media_marker_missing_is_classified_without_leak(self):
        completion = json.dumps(
            {"choices": [{"message": {"content": "synthetic completion without media marker"}}]}
        )
        self._capture_generation([completion])

        created = await self.client.post(
            "/v1/videos",
            headers=self._auth_headers(),
            data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
        )
        self.assertEqual(200, created.status_code)
        polled = await self._poll_video_terminal(created.json()["id"])

        self.assertEqual("failed", polled["status"])
        self.assertEqual("media_marker_missing", polled["error"]["code"])
        self.assertNotIn("synthetic completion without media marker", json.dumps(polled))

    async def test_video_media_proxy_connect_failure_is_classified_without_leak(self):
        remote_uri = "https://flow-content.google/synthetic-connect-failure.mp4"
        completion = json.dumps(
            {"choices": [{"message": {"content": f"<video src='{remote_uri}' controls></video>"}}]}
        )
        self._capture_generation([completion])

        async def failing_download(url, media_type, **kwargs):
            self.assertFalse(kwargs.get("log_source_url", True))
            self.assertTrue(kwargs.get("require_direct_connection"))
            raise RuntimeError("remote_media_proxy_connect_failed")

        with patch.object(
            generation_handler.file_cache,
            "download_and_cache",
            side_effect=failing_download,
        ):
            created = await self.client.post(
                "/v1/videos",
                headers=self._auth_headers(),
                data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
            )
            self.assertEqual(200, created.status_code)
            polled = await self._poll_video_terminal(created.json()["id"])

        self.assertEqual("failed", polled["status"])
        self.assertEqual("media_proxy_connect_failed", polled["error"]["code"])
        self.assertNotIn(remote_uri, json.dumps(polled))

    async def test_video_direct_download_failure_classes_are_stable_and_private(self):
        remote_uri = "https://flow-content.google/synthetic-direct-failure.mp4"
        completion = json.dumps(
            {"choices": [{"message": {"content": f"<video src='{remote_uri}' controls></video>"}}]}
        )
        self._capture_generation([completion])
        cases = (
            ("remote_media_proxy_unavailable", "media_proxy_unavailable"),
            ("remote_media_proxy_tls_failed", "media_tls_failed"),
            ("remote_media_target_rejected", "media_target_rejected"),
            ("remote_media_dns_failed", "media_dns_failed"),
            ("remote_media_dns_non_public_rejected", "media_dns_non_public_rejected"),
            ("remote_media_dns_no_public_address", "media_dns_no_public_address"),
            ("remote_media_pinned_dns_failed", "media_pinned_dns_failed"),
            ("remote_media_connect_failed", "media_connect_failed"),
            ("remote_media_tls_failed", "media_tls_failed"),
            ("remote_media_http_failed", "media_http_failed"),
            ("remote_media_empty_download", "media_empty_download"),
            ("remote_media_download_failed", "media_download_failed"),
            ("private-low-level-detail", "media_download_failed"),
        )

        for internal_code, public_code in cases:
            with self.subTest(internal_code=internal_code):
                async def failing_download(url, media_type, _code=internal_code, **kwargs):
                    self.assertFalse(kwargs.get("log_source_url", True))
                    self.assertTrue(kwargs.get("require_direct_connection"))
                    raise RuntimeError(_code)

                with patch.object(
                    generation_handler.file_cache,
                    "download_and_cache",
                    side_effect=failing_download,
                ):
                    created = await self.client.post(
                        "/v1/videos",
                        headers=self._auth_headers(),
                        data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
                    )
                    self.assertEqual(200, created.status_code)
                    polled = await self._poll_video_terminal(created.json()["id"])

                self.assertEqual("failed", polled["status"])
                self.assertEqual(public_code, polled["error"]["code"])
                serialized = json.dumps(polled)
                self.assertNotIn(remote_uri, serialized)
                self.assertNotIn(internal_code, serialized)

    async def test_video_remote_media_download_failure_reaches_stable_failed_state(self):
        remote_uri = "https://provider.invalid/synthetic-cache-failure.mp4"
        original_download = generation_handler.file_cache.download_and_cache

        async def failing_download_and_cache(url, media_type, **kwargs):
            self.assertFalse(kwargs.get("log_source_url", True))
            self.assertTrue(kwargs.get("require_direct_connection"))
            raise RuntimeError("remote_media_download_failed")

        generation_handler.file_cache.download_and_cache = failing_download_and_cache
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "download_and_cache",
            original_download,
        )
        completion = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": f"```html\n<video src='{remote_uri}' controls></video>\n```"
                        }
                    }
                ]
            }
        )
        self._capture_generation([completion])

        created = await self.client.post(
            "/v1/videos",
            headers=self._auth_headers(),
            data={"model": "omni", "prompt": "synthetic cache failure prompt", "seconds": "10"},
        )
        self.assertEqual(200, created.status_code)
        task_id = created.json()["id"]

        polled = None
        for _ in range(20):
            response = await self.client.get(
                f"/v1/videos/{task_id}", headers=self._auth_headers()
            )
            polled = response.json()
            if polled["status"] == "failed":
                break
            await asyncio.sleep(0)

        self.assertIsNotNone(polled)
        self.assertEqual("failed", polled["status"])
        self.assertEqual("media_download_failed", polled["error"]["code"])
        serialized = json.dumps(polled)
        self.assertNotIn(remote_uri, serialized)
        self.assertNotIn("remote_media_download_failed", serialized)

    async def test_video_media_cache_missing_is_classified(self):
        with patch.object(
            generation_handler.file_cache,
            "download_and_cache",
            return_value="yingce-contract-missing-cache.mp4",
        ):
            with self.assertRaisesRegex(ValueError, "^media_cache_missing$"):
                await yingce_adapter._materialize_video_content(
                    "<video src='https://flow-content.google/synthetic.mp4'></video>",
                    generation_handler,
                )

    async def test_video_remote_media_proxy_rejection_is_stable_and_does_not_leak_url(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        original_cache_dir = generation_handler.file_cache.cache_dir
        original_proxy_manager = generation_handler.file_cache.proxy_manager
        generation_handler.file_cache.cache_dir = Path(temp_dir.name)
        generation_handler.file_cache.proxy_manager = _SyntheticUnsupportedMediaProxyManager()
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "cache_dir",
            original_cache_dir,
        )
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "proxy_manager",
            original_proxy_manager,
        )
        _SyntheticRemoteSession.init_kwargs = []
        _SyntheticRemoteSession.get_calls = []
        remote_uri = "https://flow-content.google/synthetic-video.mp4"
        completion = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": f"<video src='{remote_uri}' controls></video>"
                        }
                    }
                ]
            }
        )
        self._capture_generation([completion])
        public_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.97", 443))
        ]

        with patch("socket.getaddrinfo", return_value=public_dns):
            with patch("src.services.file_cache.AsyncSession", _SyntheticRemoteSession):
                created = await self.client.post(
                    "/v1/videos",
                    headers=self._auth_headers(),
                    data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
                )
                self.assertEqual(200, created.status_code)
                task_id = created.json()["id"]

                polled = None
                for _ in range(20):
                    response = await self.client.get(
                        f"/v1/videos/{task_id}", headers=self._auth_headers()
                    )
                    polled = response.json()
                    if polled["status"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0)

                self.assertIsNotNone(polled)
                self.assertEqual("failed", polled["status"])
                self.assertEqual("media_proxy_unsupported", polled["error"]["code"])
                self.assertNotIn(remote_uri, json.dumps(polled))
                task = await yingce_adapter.video_tasks.get(task_id)
                self.assertIsNotNone(task)
                self.assertIsNone(task.filename)

        self.assertEqual([], _SyntheticRemoteSession.init_kwargs)
        self.assertEqual([], _SyntheticRemoteSession.get_calls)

    async def test_video_remote_media_http_proxy_uses_trusted_hostname_tunnel_and_local_content(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        original_cache_dir = generation_handler.file_cache.cache_dir
        original_proxy_manager = generation_handler.file_cache.proxy_manager
        generation_handler.file_cache.cache_dir = Path(temp_dir.name)
        generation_handler.file_cache.proxy_manager = _SyntheticMediaProxyManager()
        self.addCleanup(setattr, generation_handler.file_cache, "cache_dir", original_cache_dir)
        self.addCleanup(setattr, generation_handler.file_cache, "proxy_manager", original_proxy_manager)

        remote_uri = "https://flow-content.google/synthetic-video.mp4"
        completion = json.dumps(
            {"choices": [{"message": {"content": f"<video src='{remote_uri}'></video>"}}]}
        )
        self._capture_generation([completion])
        tunnel_calls = []

        async def fake_tunnel(url, **kwargs):
            tunnel_calls.append(
                (
                    kwargs["host"],
                    kwargs["address"],
                    kwargs["resolve_origin_via_proxy"],
                )
            )
            return 200, {}, b"synthetic-remote-video-bytes"

        public_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.97", 443))
        ]
        with patch("socket.getaddrinfo", return_value=public_dns):
            with patch.object(
                generation_handler.file_cache,
                "_download_remote_via_pinned_http_proxy",
                side_effect=fake_tunnel,
            ):
                created = await self.client.post(
                    "/v1/videos",
                    headers=self._auth_headers(),
                    data={"model": "omni", "prompt": "synthetic", "seconds": "10"},
                )
                self.assertEqual(200, created.status_code)
                task_id = created.json()["id"]

                polled = None
                for _ in range(20):
                    response = await self.client.get(
                        f"/v1/videos/{task_id}", headers=self._auth_headers()
                    )
                    polled = response.json()
                    if polled["status"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0)

                self.assertIsNotNone(polled)
                self.assertEqual("completed", polled["status"])
                self.assertNotIn(remote_uri, json.dumps(polled))
                task = await yingce_adapter.video_tasks.get(task_id)
                self.assertIsNotNone(task)
                self.assertIsNotNone(task.filename)
                self.assertEqual(Path(task.filename).name, task.filename)
                self.assertNotIn("://", task.filename)
                self.assertTrue((Path(temp_dir.name) / task.filename).is_file())

                content = await self.client.get(
                    f"/v1/videos/{task_id}/content", headers=self._auth_headers()
                )
                self.assertEqual(200, content.status_code)
                self.assertEqual(b"synthetic-remote-video-bytes", content.content)

        self.assertEqual([("flow-content.google", None, True)], tunnel_calls)

    async def test_video_remote_media_is_cached_before_local_content_proxy(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        original_cache_dir = generation_handler.file_cache.cache_dir
        generation_handler.file_cache.cache_dir = Path(temp_dir.name)
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "cache_dir",
            original_cache_dir,
        )
        remote_uri = "https://provider.invalid/synthetic-private-video.mp4"
        cached_filename = "cached-provider-video.mp4"
        cached_bytes = b"synthetic-cached-provider-video"
        download_calls = []
        original_download = generation_handler.file_cache.download_and_cache

        async def fake_download_and_cache(url, media_type, **kwargs):
            download_calls.append(
                (
                    url,
                    media_type,
                    kwargs.get("log_source_url"),
                    kwargs.get("require_direct_connection"),
                )
            )
            (Path(temp_dir.name) / cached_filename).write_bytes(cached_bytes)
            return cached_filename

        generation_handler.file_cache.download_and_cache = fake_download_and_cache
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "download_and_cache",
            original_download,
        )
        completion = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```html\n"
                                f"<video src='{remote_uri}' controls></video>\n"
                                "```"
                            )
                        }
                    }
                ]
            }
        )
        self._capture_generation([completion])

        created = await self.client.post(
            "/v1/videos",
            headers=self._auth_headers(),
            data={"model": "omni", "prompt": "synthetic remote media prompt", "seconds": "10"},
        )
        self.assertEqual(200, created.status_code)
        task_id = created.json()["id"]

        polled = None
        for _ in range(20):
            response = await self.client.get(
                f"/v1/videos/{task_id}", headers=self._auth_headers()
            )
            self.assertEqual(200, response.status_code)
            polled = response
            if response.json()["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0)

        self.assertIsNotNone(polled)
        self.assertEqual("completed", polled.json()["status"])
        self.assertNotIn(remote_uri, polled.text)
        self.assertEqual([(remote_uri, "video", False, True)], download_calls)
        content = await self.client.get(
            f"/v1/videos/{task_id}/content", headers=self._auth_headers()
        )
        self.assertEqual(200, content.status_code)
        self.assertEqual(cached_bytes, content.content)

    async def test_video_idempotency_reuses_same_task_and_conflicts_on_changed_request(self):
        completion = json.dumps(
            {
                "error": {
                    "message": "synthetic idempotency failure",
                    "code": "upstream_error",
                    "status_code": 502,
                }
            }
        )
        calls = self._capture_generation([completion])
        headers = {
            **self._auth_headers(),
            "Idempotency-Key": "yingce-idempotency-fixture",
        }
        payload = {
            "model": "omni",
            "prompt": "synthetic idempotent prompt",
            "seconds": "10",
            "size": "1792x1024",
        }

        first = await self.client.post("/v1/videos", headers=headers, data=payload)
        second = await self.client.post("/v1/videos", headers=headers, data=payload)

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(first.json()["id"], second.json()["id"])

        conflict = await self.client.post(
            "/v1/videos",
            headers=headers,
            data={**payload, "prompt": "synthetic changed prompt"},
        )
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("idempotency_conflict", conflict.json()["error"]["code"])

        await asyncio.sleep(0)
        self.assertEqual(1, len(calls))

    async def test_video_content_rejects_registry_path_traversal(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        cache_dir = Path(temp_dir.name) / "cache"
        cache_dir.mkdir()
        outside_path = Path(temp_dir.name) / "outside.mp4"
        outside_path.write_bytes(b"synthetic-outside-bytes")
        original_cache_dir = generation_handler.file_cache.cache_dir
        generation_handler.file_cache.cache_dir = cache_dir
        self.addCleanup(
            setattr,
            generation_handler.file_cache,
            "cache_dir",
            original_cache_dir,
        )

        task = await yingce_adapter.video_tasks.create(
            model="omni", size=None, seconds=10
        )
        await yingce_adapter.video_tasks.update(
            task.id,
            status="completed",
            progress=100,
            filename="../outside.mp4",
        )

        response = await self.client.get(
            f"/v1/videos/{task.id}/content", headers=self._auth_headers()
        )
        self.assertEqual(404, response.status_code)
        self.assertEqual("video_content_missing", response.json()["error"]["code"])
        self.assertNotIn("synthetic-outside-bytes", response.text)

    async def test_video_failed_state_uses_stable_error_without_raw_provider_detail(self):
        raw_detail = "synthetic-private-provider-detail"
        completion = json.dumps(
            {
                "error": {
                    "message": raw_detail,
                    "code": "raw_provider_failure_fixture",
                    "status_code": 502,
                }
            }
        )
        self._capture_generation([completion])

        created = await self.client.post(
            "/v1/videos",
            headers=self._auth_headers(),
            data={
                "model": "omni",
                "prompt": "synthetic private prompt",
                "seconds": "10",
            },
        )
        self.assertEqual(200, created.status_code)
        task_id = created.json()["id"]

        polled = None
        for _ in range(20):
            response = await self.client.get(
                f"/v1/videos/{task_id}", headers=self._auth_headers()
            )
            self.assertEqual(200, response.status_code)
            polled = response
            if response.json()["status"] == "failed":
                break
            await asyncio.sleep(0)

        self.assertIsNotNone(polled)
        payload = polled.json()
        self.assertEqual("failed", payload["status"])
        self.assertEqual("generation_failed", payload["error"]["code"])

        content = await self.client.get(
            f"/v1/videos/{task_id}/content", headers=self._auth_headers()
        )
        self.assertEqual(409, content.status_code)
        self.assertEqual("generation_failed", content.json()["error"]["code"])
        self.assertNotIn(raw_detail, content.text)

        serialized = polled.text
        self.assertNotIn(raw_detail, serialized)
        self.assertNotIn("synthetic private prompt", serialized)
        self.assertNotIn("yingce-synthetic-api-key", serialized)

    async def test_existing_base_capability_ids_remain_discoverable(self):
        response = await self.client.get(
            "/v1/models", headers=self._auth_headers()
        )
        self.assertEqual(200, response.status_code)
        data = response.json()["data"]
        self.assertTrue(
            {
                "nano-banana-2",
                "nano-banana-pro",
                "omni-flash",
                "veo-3.1-lite",
                "veo-3.1-fast",
                "veo-3.1-quality",
            }.issubset({item["capability_id"] for item in data})
        )

    async def test_existing_chat_completions_contract_still_uses_generation_handler(self):
        completion = json.dumps(
            {
                "id": "chatcmpl-existing-contract",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "synthetic existing chat response",
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )
        calls = self._capture_generation([completion])

        response = await self.client.post(
            "/v1/chat/completions",
            headers=self._auth_headers(),
            json={
                "model": "gemini-3.1-flash-image",
                "messages": [
                    {"role": "user", "content": "synthetic existing chat prompt"}
                ],
                "stream": False,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("chatcmpl-existing-contract", response.json()["id"])
        self.assertEqual(1, len(calls))
        self.assertEqual("gemini-3.1-flash-image-landscape", calls[0]["model"])
        self.assertEqual("synthetic existing chat prompt", calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
