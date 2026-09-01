import unittest

from src.core.public_model_catalog import build_public_model_catalog
from src.services.generation_handler import MODEL_CONFIG


class OmniOneOneCompatibilityTests(unittest.TestCase):
    def test_new_omni_one_one_aliases_reuse_verified_flow_keys(self):
        expected = {
            "omni_1_1": (
                "omni",
                "abra_t2v_8s",
                "abra_r2v_8s",
                "VIDEO_ASPECT_RATIO_LANDSCAPE",
            ),
            "omni_1_1_portrait": (
                "omni",
                "abra_t2v_8s",
                "abra_r2v_8s",
                "VIDEO_ASPECT_RATIO_PORTRAIT",
            ),
            "omni_1_1_10s": (
                "t2v",
                "abra_t2v_10s",
                None,
                "VIDEO_ASPECT_RATIO_LANDSCAPE",
            ),
            "omni_1_1_portrait_10s": (
                "t2v",
                "abra_t2v_10s",
                None,
                "VIDEO_ASPECT_RATIO_PORTRAIT",
            ),
        }

        for model_id, values in expected.items():
            with self.subTest(model_id=model_id):
                video_type, model_key, reference_key, aspect_ratio = values
                config = MODEL_CONFIG[model_id]
                self.assertEqual("video", config["type"])
                self.assertEqual(video_type, config["video_type"])
                self.assertEqual(model_key, config["model_key"])
                self.assertEqual(aspect_ratio, config["aspect_ratio"])
                self.assertFalse(config.get("allow_tier_upgrade", True))
                if reference_key:
                    self.assertEqual(reference_key, config["reference_model_key"])
                    self.assertTrue(config["supports_images"])
                    self.assertEqual(3, config["max_images"])
                else:
                    self.assertNotIn("reference_model_key", config)
                    self.assertFalse(config["supports_images"])

    def test_catalog_adds_omni_one_one_without_mutating_legacy_omni(self):
        catalog = {
            entry["capability_id"]: entry
            for entry in build_public_model_catalog(MODEL_CONFIG)
        }

        legacy = catalog["omni-flash"]
        self.assertEqual("Omni Flash · 文生视频", legacy["display_name"])
        self.assertEqual(
            {
                ("16:9", "8", "omni"),
                ("9:16", "8", "omni_portrait"),
                ("16:9", "10", "omni_10s"),
                ("9:16", "10", "omni_portrait_10s"),
            },
            {
                (
                    item["parameters"]["aspect_ratio"],
                    item["parameters"]["duration_seconds"],
                    item["model_id"],
                )
                for item in legacy["compatibility_map"]
            },
        )

        text = catalog["omni-1.1-flash"]
        self.assertEqual("Omni 1.1 Flash · 文生视频", text["display_name"])
        self.assertEqual("10", text["default_parameters"]["duration_seconds"])
        self.assertEqual(
            {
                ("16:9", "8", "omni_1_1"),
                ("9:16", "8", "omni_1_1_portrait"),
                ("16:9", "10", "omni_1_1_10s"),
                ("9:16", "10", "omni_1_1_portrait_10s"),
            },
            {
                (
                    item["parameters"]["aspect_ratio"],
                    item["parameters"]["duration_seconds"],
                    item["model_id"],
                )
                for item in text["compatibility_map"]
            },
        )

        references = catalog["omni-1.1-flash-references"]
        self.assertEqual(
            ("references_to_video", 1, 3),
            (
                references["generation_mode"],
                references["min_images"],
                references["max_images"],
            ),
        )
        self.assertEqual(
            {("16:9", "8", "omni_1_1"), ("9:16", "8", "omni_1_1_portrait")},
            {
                (
                    item["parameters"]["aspect_ratio"],
                    item["parameters"]["duration_seconds"],
                    item["model_id"],
                )
                for item in references["compatibility_map"]
            },
        )


if __name__ == "__main__":
    unittest.main()
