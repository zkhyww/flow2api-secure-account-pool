import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler


class ExtendPrivacyContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_keeps_extend_source_out_of_stream_text_and_debug_logs(self):
        handler = GenerationHandler(SimpleNamespace(), None, None, None, None, None)
        token = SimpleNamespace(
            id=7,
            at="private-access",
            user_paygate_tier="PAYGATE_TIER_ONE",
            video_concurrency=1,
        )
        model_config = {
            "video_type": "extend",
            "supports_images": False,
            "min_images": 0,
            "max_images": 0,
            "use_v2_model_config": False,
            "model_key": "veo_3_1_extend_landscape",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "allow_tier_upgrade": False,
        }
        private_id = "private-media-generation-id"
        chunks = []

        with patch(
            "src.services.generation_handler.debug_logger.log_info"
        ) as log_info:
            stream = handler._handle_video_generation(
                token,
                "private-project",
                model_config,
                "private-prompt",
                None,
                True,
                video_media_id=private_id,
            )
            try:
                chunks.append(await anext(stream))
                chunks.append(await anext(stream))
            finally:
                await stream.aclose()

        emitted = "\n".join(chunks)
        logged = "\n".join(str(call) for call in log_info.call_args_list)
        self.assertNotIn(private_id, emitted)
        self.assertNotIn(private_id[:8], emitted)
        self.assertNotIn(private_id, logged)
        self.assertNotIn(private_id[:8], logged)
        self.assertIn("源视频已安全选择", emitted)

    async def test_flow_client_does_not_log_extend_request_payload_or_private_values(self):
        flow = FlowClient(proxy_manager=None)
        flow._acquire_video_launch_gate = AsyncMock(return_value=(True, 0, 0))
        flow._release_video_launch_gate = AsyncMock()
        flow._get_recaptcha_token = AsyncMock(
            return_value=("private-captcha", "private-browser")
        )
        flow._warmup_flow_video_frontend_context = AsyncMock()
        flow._make_video_api_request = AsyncMock(
            return_value={"operations": [{"operation": {"name": "private-operation"}}]}
        )
        flow._notify_browser_captcha_request_finished = AsyncMock()

        with patch("src.services.flow_client.debug_logger.log_info") as log_info:
            await flow.generate_video_extend(
                at="private-access",
                project_id="private-project",
                prompt="private-prompt",
                model_key="veo_3_1_extend_landscape",
                aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
                video_media_id="private-media-generation-id",
                token_id=7,
            )

        logged = "\n".join(str(call) for call in log_info.call_args_list)
        for private_value in (
            "private-access",
            "private-project",
            "private-prompt",
            "private-captcha",
            "private-media-generation-id",
            "private-operation",
        ):
            self.assertNotIn(private_value, logged)
        self.assertNotIn("://", logged)

    async def test_concatenation_logs_only_status_not_ids_urls_or_raw_responses(self):
        flow = FlowClient(proxy_manager=None)
        flow._make_request = AsyncMock(
            side_effect=[
                {"operation": {"operation": {"name": "private-operation"}}},
                {
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
                    "outputUri": "https://private.example/media",
                    "private": "private-response-value",
                },
            ]
        )

        with patch("src.services.flow_client.debug_logger.log_info") as log_info:
            await flow.run_concatenation(
                at="private-access",
                original_media_id="private-original-media-id",
                extend_media_id="private-extend-media-id",
            )
            await flow.poll_concatenation_status(
                at="private-access",
                operation_name="private-operation",
                timeout=1,
                poll_interval=0,
            )

        logged = "\n".join(str(call) for call in log_info.call_args_list)
        for private_value in (
            "private-original-media-id",
            "private-extend-media-id",
            "private-operation",
            "private-response-value",
            "https://private.example/media",
        ):
            self.assertNotIn(private_value, logged)
        self.assertNotIn("://", logged)


if __name__ == "__main__":
    unittest.main()
