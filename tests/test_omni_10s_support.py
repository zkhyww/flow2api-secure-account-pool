import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.public_model_catalog import build_public_model_catalog
from src.services.generation_handler import MODEL_CONFIG, GenerationHandler


class OmniTenSecondModelConfigTests(unittest.TestCase):
    def test_ten_second_landscape_and_portrait_are_text_only_compatible_models(self):
        expected = {
            "omni_10s": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            "omni_portrait_10s": "VIDEO_ASPECT_RATIO_PORTRAIT",
        }

        for model_id, aspect_ratio in expected.items():
            with self.subTest(model_id=model_id):
                config = MODEL_CONFIG[model_id]
                self.assertEqual("video", config["type"])
                self.assertEqual("t2v", config["video_type"])
                self.assertEqual("abra_t2v_10s", config["model_key"])
                self.assertEqual(aspect_ratio, config["aspect_ratio"])
                self.assertFalse(config["supports_images"])
                self.assertEqual(0, config.get("min_images", 0))
                self.assertEqual(0, config.get("max_images", 0))
                self.assertFalse(config.get("allow_tier_upgrade", True))
                self.assertNotIn("reference_model_key", config)

    def test_catalog_exposes_validated_omni_eight_and_ten_seconds_with_ten_as_default(self):
        catalog = {
            entry["capability_id"]: entry
            for entry in build_public_model_catalog(MODEL_CONFIG)
        }
        omni = catalog["omni-flash"]

        self.assertEqual("10", omni["default_parameters"]["duration_seconds"])
        duration_options = {
            option["value"]: option
            for option in omni["options"]["duration_seconds"]
        }
        self.assertEqual({"8", "10"}, set(duration_options))
        self.assertEqual("validated", duration_options["8"]["validation_status"])
        self.assertEqual("validated", duration_options["10"]["validation_status"])

        mappings = {
            (
                item["parameters"]["aspect_ratio"],
                item["parameters"]["duration_seconds"],
            ): item
            for item in omni["compatibility_map"]
        }
        self.assertEqual(
            {
                ("16:9", "8"): ("omni", "validated"),
                ("9:16", "8"): ("omni_portrait", "validated"),
                ("16:9", "10"): ("omni_10s", "validated"),
                ("9:16", "10"): ("omni_portrait_10s", "validated"),
            },
            {
                key: (item["model_id"], item["validation_status"])
                for key, item in mappings.items()
            },
        )

        for capability_id in (
            "veo-3.1-lite",
            "veo-3.1-fast",
            "veo-3.1-quality",
        ):
            entry = catalog[capability_id]
            self.assertEqual("8", entry["default_parameters"]["duration_seconds"])
            self.assertEqual(
                {"8"},
                {
                    option["value"]
                    for option in entry["options"]["duration_seconds"]
                },
            )
            self.assertTrue(
                all(
                    mapping["parameters"]["duration_seconds"] == "8"
                    and mapping["validation_status"] == "validated"
                    for mapping in entry["compatibility_map"]
                )
            )

        all_durations = {
            mapping["parameters"]["duration_seconds"]
            for entry in catalog.values()
            if entry["model_type"] == "video"
            for mapping in entry["compatibility_map"]
        }
        self.assertEqual({"8", "10"}, all_durations)


class OmniTenSecondGenerationHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_second_model_uses_existing_text_video_path_with_exact_model_key(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = SimpleNamespace(
            generate_video_text=AsyncMock(return_value={"operations": []}),
            generate_video_reference_images=AsyncMock(),
        )
        handler._update_request_log_progress = AsyncMock()

        token = SimpleNamespace(
            id=17,
            at="test-access",
            user_paygate_tier="PAYGATE_TIER_ONE",
            video_concurrency=1,
        )
        chunks = [
            chunk
            async for chunk in handler._handle_video_generation(
                token,
                "test-project",
                MODEL_CONFIG["omni_10s"],
                "test-prompt",
                None,
                False,
            )
        ]

        self.assertTrue(chunks)
        handler.flow_client.generate_video_text.assert_awaited_once()
        call = handler.flow_client.generate_video_text.await_args
        self.assertEqual("abra_t2v_10s", call.kwargs["model_key"])
        self.assertEqual(
            "VIDEO_ASPECT_RATIO_LANDSCAPE",
            call.kwargs["aspect_ratio"],
        )
        handler.flow_client.generate_video_reference_images.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
