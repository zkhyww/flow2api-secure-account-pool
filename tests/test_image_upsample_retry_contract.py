import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config import config
from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler


class ImageUpsampleRetryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_failure_does_not_replay_each_upsample_post_via_urllib(self):
        flow = FlowClient(proxy_manager=None)
        flow._get_recaptcha_token = AsyncMock(
            side_effect=[
                ("captcha-fixture-1", None),
                ("captcha-fixture-2", None),
                ("captcha-fixture-3", None),
            ]
        )
        flow._notify_browser_captcha_request_finished = AsyncMock()
        flow._handle_retryable_generation_error = AsyncMock(
            side_effect=lambda **kwargs: kwargs["retry_attempt"] < kwargs["max_retries"] - 1
        )
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.__aexit__.return_value = False
        session.post.side_effect = RuntimeError("transport failed after write")
        urllib_fallback = MagicMock(return_value={"encodedImage": "unexpected"})
        old_retries = config.flow_max_retries
        config.set_flow_max_retries(3)
        try:
            with patch("src.services.flow_client.AsyncSession", return_value=session), patch.object(
                flow,
                "_should_fallback_to_urllib",
                return_value=True,
            ), patch.object(flow, "_sync_json_request_via_urllib", urllib_fallback):
                with self.assertRaisesRegex(Exception, "transport failed"):
                    await flow.upsample_image(
                        at="access-fixture",
                        project_id="project-fixture",
                        media_id="media-fixture",
                        session_id="generation-context",
                        token_id=7,
                    )
        finally:
            config.set_flow_max_retries(old_retries)

        self.assertEqual(3, session.post.await_count)
        urllib_fallback.assert_not_called()

    async def test_flow_client_owns_three_total_submissions_with_fresh_tokens_and_stable_context(self):
        flow = FlowClient(proxy_manager=None)
        captcha_calls = []
        submissions = []

        async def get_captcha(project_id, action="IMAGE_GENERATION", token_id=None, session_id=None):
            captcha_calls.append((project_id, action, token_id, session_id))
            return f"captcha-fixture-{len(captcha_calls)}", None

        async def submit(**kwargs):
            context = kwargs["json_data"]["clientContext"]
            submissions.append(
                (
                    context["recaptchaContext"]["token"],
                    context["projectId"],
                    context["sessionId"],
                )
            )
            raise Exception("reCAPTCHA evaluation failed")

        flow._get_recaptcha_token = get_captcha
        flow._make_request = submit
        flow._notify_browser_captcha_request_finished = AsyncMock()
        flow._handle_retryable_generation_error = AsyncMock(
            side_effect=lambda **kwargs: kwargs["retry_attempt"] < kwargs["max_retries"] - 1
        )
        old_retries = config.flow_max_retries
        config.set_flow_max_retries(3)
        try:
            with self.assertRaisesRegex(Exception, "reCAPTCHA"):
                await flow.upsample_image(
                    at="access-fixture",
                    project_id="project-fixture",
                    media_id="media-fixture",
                    session_id="generation-context",
                    token_id=7,
                )
        finally:
            config.set_flow_max_retries(old_retries)

        self.assertEqual(3, len(submissions))
        self.assertEqual(3, len({entry[0] for entry in submissions}))
        self.assertEqual({"project-fixture"}, {entry[1] for entry in submissions})
        self.assertEqual({"generation-context"}, {entry[2] for entry in submissions})
        self.assertEqual(
            [("project-fixture", "IMAGE_GENERATION", 7, "generation-context")] * 3,
            captcha_calls,
        )

    async def test_generation_handler_calls_upsample_once_and_marks_original_fallback(self):
        flow = SimpleNamespace(
            generate_image=AsyncMock(
                return_value=(
                    {
                        "media": [
                            {
                                "name": "media-fixture",
                                "image": {"generatedImage": {"fifeUrl": "origin-fixture"}},
                            }
                        ]
                    },
                    "generation-context",
                    {},
                )
            ),
            upsample_image=AsyncMock(side_effect=Exception("reCAPTCHA evaluation failed")),
            _get_retry_reason=lambda _error: "reCAPTCHA validation failed",
        )
        handler = GenerationHandler(flow, None, None, None, None, None)
        token = SimpleNamespace(
            id=7,
            at="access-fixture",
            user_paygate_tier="PAYGATE_TIER_NOT_PAID",
            image_concurrency=1,
        )
        response_state = handler._create_response_state()
        generation_result = handler._create_generation_result()
        model_config = {
            "model_name": "GEM_PIX",
            "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
            "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K",
        }

        with patch("src.services.generation_handler.asyncio.sleep", new=AsyncMock()), patch.object(
            config,
            "_config",
            {**config._config, "cache": {**config._config.get("cache", {}), "enabled": False}},
        ):
            chunks = [
                chunk
                async for chunk in handler._handle_image_generation(
                    token,
                    "project-fixture",
                    model_config,
                    "",
                    None,
                    False,
                    response_state=response_state,
                    generation_result=generation_result,
                )
            ]

        flow.upsample_image.assert_awaited_once()
        self.assertTrue(chunks)
        self.assertEqual("origin-fixture", response_state["url"])
        assets = response_state["generated_assets"]
        self.assertEqual("original_fallback", assets["delivery_mode"])
        self.assertEqual("failed", assets["upsample_status"])
        self.assertNotIn("2K", repr(assets))


if __name__ == "__main__":
    unittest.main()
