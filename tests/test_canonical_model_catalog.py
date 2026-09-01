import types
import unittest

import httpx
from fastapi import FastAPI

from src.api import admin, routes
from src.core.config import config
from src.core.model_resolver import resolve_model_name
from src.core.public_model_catalog import build_public_model_catalog
from src.services.generation_handler import MODEL_CONFIG


EXPECTED_CAPABILITY_IDS = {
    "nano-banana-2",
    "nano-banana-pro",
    "omni-flash",
    "omni-flash-references",
    "omni-1.1-flash",
    "omni-1.1-flash-references",
    "veo-3.1-lite",
    "veo-3.1-lite-first-frame",
    "veo-3.1-lite-first-last",
    "veo-3.1-fast",
    "veo-3.1-fast-first-frame",
    "veo-3.1-fast-first-last",
    "veo-3.1-fast-references",
    "veo-3.1-quality",
    "veo-3.1-quality-first-frame",
    "veo-3.1-quality-first-last",
}
EXPECTED_IMAGE_CAPABILITIES = {"nano-banana-2", "nano-banana-pro"}
EXPECTED_VIDEO_CAPABILITIES = EXPECTED_CAPABILITY_IDS - EXPECTED_IMAGE_CAPABILITIES
IMAGE_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4"}
IMAGE_RESOLUTIONS = {"1K", "2K", "4K"}
VIDEO_RATIOS = {"16:9", "9:16"}

EXPECTED_VIDEO_SELECTIONS = {
    "omni-flash": {
        "mode": "text_to_video",
        "label": "文生视频",
        "images": (0, 0),
        "mappings": {
            ("16:9", "8"): "omni",
            ("9:16", "8"): "omni_portrait",
            ("16:9", "10"): "omni_10s",
            ("9:16", "10"): "omni_portrait_10s",
        },
    },
    "omni-flash-references": {
        "mode": "references_to_video",
        "label": "参考图生视频",
        "images": (1, 3),
        "mappings": {
            ("16:9", "8"): "omni",
            ("9:16", "8"): "omni_portrait",
        },
    },
    "omni-1.1-flash": {
        "mode": "text_to_video",
        "label": "文生视频",
        "images": (0, 0),
        "mappings": {
            ("16:9", "8"): "omni_1_1",
            ("9:16", "8"): "omni_1_1_portrait",
            ("16:9", "10"): "omni_1_1_10s",
            ("9:16", "10"): "omni_1_1_portrait_10s",
        },
    },
    "omni-1.1-flash-references": {
        "mode": "references_to_video",
        "label": "参考图生视频",
        "images": (1, 3),
        "mappings": {
            ("16:9", "8"): "omni_1_1",
            ("9:16", "8"): "omni_1_1_portrait",
        },
    },
    "veo-3.1-lite": {
        "mode": "text_to_video",
        "label": "文生视频",
        "images": (0, 0),
        "mappings": {
            ("16:9", "8"): "veo_3_1_t2v_lite_landscape_8s",
            ("9:16", "8"): "veo_3_1_t2v_lite_portrait_8s",
        },
    },
    "veo-3.1-lite-first-frame": {
        "mode": "first_frame_to_video",
        "label": "首帧生视频",
        "images": (1, 1),
        "mappings": {
            ("16:9", "8"): "veo_3_1_i2v_lite_landscape_8s",
            ("9:16", "8"): "veo_3_1_i2v_lite_portrait_8s",
        },
    },
    "veo-3.1-lite-first-last": {
        "mode": "first_last_frame_to_video",
        "label": "首尾帧生视频",
        "images": (2, 2),
        "mappings": {
            ("16:9", "8"): "veo_3_1_interpolation_lite_landscape_8s",
            ("9:16", "8"): "veo_3_1_interpolation_lite_portrait_8s",
        },
    },
    "veo-3.1-fast": {
        "mode": "text_to_video",
        "label": "文生视频",
        "images": (0, 0),
        "mappings": {
            ("16:9", "8"): "veo_3_1_t2v_fast_landscape_8s",
            ("9:16", "8"): "veo_3_1_t2v_fast_portrait_8s",
        },
    },
    "veo-3.1-fast-first-frame": {
        "mode": "first_frame_to_video",
        "label": "首帧生视频",
        "images": (1, 1),
        "mappings": {
            ("16:9", "8"): "veo_3_1_i2v_s_fast_landscape_8s_fl",
            ("9:16", "8"): "veo_3_1_i2v_s_fast_portrait_8s_fl",
        },
    },
    "veo-3.1-fast-first-last": {
        "mode": "first_last_frame_to_video",
        "label": "首尾帧生视频",
        "images": (2, 2),
        "mappings": {
            ("16:9", "8"): "veo_3_1_i2v_s_fast_landscape_8s_fl",
            ("9:16", "8"): "veo_3_1_i2v_s_fast_portrait_8s_fl",
        },
    },
    "veo-3.1-fast-references": {
        "mode": "references_to_video",
        "label": "参考图生视频",
        "images": (1, 3),
        "mappings": {
            ("16:9", "8"): "veo_3_1_r2v_fast_landscape",
            ("9:16", "8"): "veo_3_1_r2v_fast_portrait",
        },
    },
    "veo-3.1-quality": {
        "mode": "text_to_video",
        "label": "文生视频",
        "images": (0, 0),
        "mappings": {
            ("16:9", "8"): "veo_3_1_t2v_landscape_8s",
            ("9:16", "8"): "veo_3_1_t2v_portrait_8s",
        },
    },
    "veo-3.1-quality-first-frame": {
        "mode": "first_frame_to_video",
        "label": "首帧生视频",
        "images": (1, 1),
        "mappings": {
            ("16:9", "8"): "veo_3_1_i2v_s_landscape_8s",
            ("9:16", "8"): "veo_3_1_i2v_s_portrait_8s",
        },
    },
    "veo-3.1-quality-first-last": {
        "mode": "first_last_frame_to_video",
        "label": "首尾帧生视频",
        "images": (2, 2),
        "mappings": {
            ("16:9", "8"): "veo_3_1_i2v_s_landscape_8s",
            ("9:16", "8"): "veo_3_1_i2v_s_portrait_8s",
        },
    },
}


def _by_capability(entries):
    return {entry["capability_id"]: entry for entry in entries}


def _option_values(entry, name):
    return {option["value"] for option in entry["options"][name]}


class CanonicalModelCatalogUnitTests(unittest.TestCase):
    def setUp(self):
        self.entries = build_public_model_catalog(MODEL_CONFIG)
        self.catalog = _by_capability(self.entries)

    def test_catalog_exposes_two_image_and_twelve_explicit_video_capabilities(self):
        self.assertEqual(EXPECTED_CAPABILITY_IDS, set(self.catalog))
        self.assertEqual(
            EXPECTED_IMAGE_CAPABILITIES,
            {
                capability_id
                for capability_id, entry in self.catalog.items()
                if entry["model_type"] == "image"
            },
        )
        self.assertEqual(
            EXPECTED_VIDEO_CAPABILITIES,
            {
                capability_id
                for capability_id, entry in self.catalog.items()
                if entry["model_type"] == "video"
            },
        )
        self.assertNotIn("veo-3.1-quality-references", self.catalog)

    def test_video_discovery_ids_are_capability_ids_and_internal_models_stay_in_compatibility_map(self):
        for capability_id in EXPECTED_VIDEO_CAPABILITIES:
            with self.subTest(capability_id=capability_id):
                entry = self.catalog[capability_id]
                self.assertEqual(capability_id, entry["id"])
                internal_model_ids = {
                    mapping["model_id"] for mapping in entry["compatibility_map"]
                }
                self.assertTrue(internal_model_ids)
                self.assertTrue(internal_model_ids.issubset(MODEL_CONFIG))
                self.assertNotIn(entry["id"], internal_model_ids)

    def test_image_options_are_parameterized_and_map_to_compatible_ids(self):
        for capability_id in EXPECTED_IMAGE_CAPABILITIES:
            entry = self.catalog[capability_id]
            self.assertEqual(IMAGE_RATIOS, _option_values(entry, "aspect_ratio"))
            self.assertEqual(IMAGE_RESOLUTIONS, _option_values(entry, "resolution"))
            self.assertEqual(15, len(entry["compatibility_map"]))
            for mapping in entry["compatibility_map"]:
                self.assertIn(mapping["model_id"], MODEL_CONFIG)
                self.assertEqual("image", MODEL_CONFIG[mapping["model_id"]]["type"])
                self.assertIn(
                    mapping["validation_status"],
                    {"validated", "membership_required", "hidden"},
                )

        nano2 = self.catalog["nano-banana-2"]
        nano2_map = {
            (item["parameters"]["aspect_ratio"], item["parameters"]["resolution"]): item["model_id"]
            for item in nano2["compatibility_map"]
        }
        self.assertEqual(
            "gemini-3.1-flash-image-four-three-2k",
            nano2_map[("4:3", "2K")],
        )
        self.assertEqual(
            "gemini-3.1-flash-image-portrait-4k",
            nano2_map[("9:16", "4K")],
        )

        pro = self.catalog["nano-banana-pro"]
        pro_map = {
            (item["parameters"]["aspect_ratio"], item["parameters"]["resolution"]): item["model_id"]
            for item in pro["compatibility_map"]
        }
        self.assertEqual(
            "gemini-3.0-pro-image-square",
            pro_map[("1:1", "1K")],
        )

    def test_image_validation_matrix_matches_codex_flow_evidence(self):
        nano2 = self.catalog["nano-banana-2"]
        nano2_status = {
            (item["parameters"]["aspect_ratio"], item["parameters"]["resolution"]): item["validation_status"]
            for item in nano2["compatibility_map"]
        }
        self.assertEqual("validated", nano2["validation_status"])
        self.assertEqual("1K", nano2["default_parameters"]["resolution"])
        for ratio in IMAGE_RATIOS:
            self.assertEqual("validated", nano2_status[(ratio, "1K")])
            self.assertEqual("validated", nano2_status[(ratio, "2K")])
            self.assertEqual("membership_required", nano2_status[(ratio, "4K")])

        pro = self.catalog["nano-banana-pro"]
        pro_status = {
            (item["parameters"]["aspect_ratio"], item["parameters"]["resolution"]): item["validation_status"]
            for item in pro["compatibility_map"]
        }
        self.assertEqual("validated", pro["validation_status"])
        self.assertEqual("1K", pro["default_parameters"]["resolution"])
        for ratio in IMAGE_RATIOS:
            self.assertEqual("validated", pro_status[(ratio, "1K")])
            self.assertEqual("membership_required", pro_status[(ratio, "4K")])
        for ratio in IMAGE_RATIOS:
            self.assertEqual("validated", pro_status[(ratio, "2K")])

        resolution_labels = {
            option["value"]: option["label"]
            for option in nano2["options"]["resolution"]
        }
        self.assertEqual(
            {
                "1K": "1K（可用）",
                "2K": "2K（可用）",
                "4K": "4K（需要高级会员）",
            },
            resolution_labels,
        )

    def test_video_capabilities_expose_exact_generation_modes_and_native_resolution(self):
        for capability_id, expected in EXPECTED_VIDEO_SELECTIONS.items():
            with self.subTest(capability_id=capability_id):
                entry = self.catalog[capability_id]
                self.assertEqual(VIDEO_RATIOS, _option_values(entry, "aspect_ratio"))
                self.assertEqual({"native"}, _option_values(entry, "resolution"))
                self.assertEqual("native", entry["default_parameters"]["resolution"])
                self.assertEqual(expected["mode"], entry["generation_mode"])
                self.assertEqual(
                    [{"id": expected["mode"], "label": expected["label"]}],
                    entry["generation_modes"],
                )
                self.assertEqual(expected["images"][0], entry["min_images"])
                self.assertEqual(expected["images"][1], entry["max_images"])
                self.assertEqual(expected["images"][1] > 0, entry["supports_images"])
                self.assertTrue(str(entry["image_semantics"]).strip())
                self.assertTrue(str(entry["usage_guide"]).strip())
                self.assertIsInstance(entry["unsupported_notes"], list)
                self.assertTrue(entry["unsupported_notes"])
                actual_map = {
                    (
                        item["parameters"]["aspect_ratio"],
                        item["parameters"]["duration_seconds"],
                    ): item["model_id"]
                    for item in entry["compatibility_map"]
                }
                self.assertEqual(expected["mappings"], actual_map)
                self.assertTrue(
                    all(
                        item["validation_status"] == "validated"
                        and item["model_id"] in MODEL_CONFIG
                        and MODEL_CONFIG[item["model_id"]]["type"] == "video"
                        for item in entry["compatibility_map"]
                    )
                )

        self.assertEqual(
            {"8", "10"},
            _option_values(self.catalog["omni-flash"], "duration_seconds"),
        )
        for capability_id in EXPECTED_VIDEO_CAPABILITIES - {
            "omni-flash",
            "omni-1.1-flash",
        }:
            self.assertEqual(
                {"8"},
                _option_values(self.catalog[capability_id], "duration_seconds"),
            )

    def test_public_image_modes_do_not_overclaim_unverified_reference_paths(self):
        self.assertIn("10 秒", " ".join(self.catalog["omni-flash-references"]["unsupported_notes"]))
        self.assertIn("Quality", " ".join(self.catalog["veo-3.1-quality-first-frame"]["unsupported_notes"]))
        self.assertNotIn("veo-3.1-quality-references", self.catalog)

    def test_extend_is_an_action_not_a_selectable_model(self):
        public_ids = {entry["id"] for entry in self.entries}
        self.assertNotIn("veo_3_1_extend", public_ids)
        self.assertNotIn("veo_3_1_extend_portrait", public_ids)

        for capability_id in EXPECTED_VIDEO_CAPABILITIES:
            actions = {
                action["id"]: action
                for action in self.catalog[capability_id].get("actions", [])
            }
            self.assertEqual({"extend"}, set(actions))
            self.assertEqual(
                {
                    "16:9": "veo_3_1_extend",
                    "9:16": "veo_3_1_extend_portrait",
                },
                actions["extend"]["model_map"],
            )
            self.assertEqual("validated", actions["extend"]["validation_status"])

    def test_legacy_aliases_remain_callable_but_are_not_discoverable(self):
        callable_aliases = {
            "veo_3_1_t2v_fast_landscape",
            "veo_3_1_t2v_fast_landscape_4s",
            "veo_3_1_t2v_lite_landscape_6s",
            "veo_3_1_t2v_landscape_4s_4k",
            "veo_3_1_t2v_lite_8s_landscape",
        }
        legacy_public_names = callable_aliases | {
            "gemini-2.5-flash-image",
            "imagen-4.0-generate-preview-06-06",
        }
        public_ids = {entry["id"] for entry in self.entries}
        compatible_ids = {
            mapping["model_id"]
            for entry in self.entries
            for mapping in entry["compatibility_map"]
        }
        self.assertTrue(legacy_public_names.isdisjoint(public_ids))
        self.assertTrue(legacy_public_names.isdisjoint(compatible_ids))
        self.assertTrue(callable_aliases.issubset(MODEL_CONFIG))

        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(
                aspectRatio="portrait",
                imageSize=None,
            )
        )
        self.assertEqual(
            "veo_3_1_t2v_fast_portrait_4s",
            resolve_model_name(
                "veo_3_1_t2v_fast_4s",
                request=request,
                model_config=MODEL_CONFIG,
            ),
        )


class CanonicalModelCatalogApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_api_key = config.api_key
        config.api_key = "canonical-catalog-api-key-fixture"
        admin.active_admin_tokens.add("canonical-catalog-admin-fixture")
        app = FastAPI()
        app.include_router(routes.router)
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        admin.active_admin_tokens.discard("canonical-catalog-admin-fixture")
        config.api_key = self.original_api_key
        await self.client.aclose()

    async def _issue_test_capability(self):
        response = await self.client.post(
            "/api/admin/test-capability",
            headers={"Authorization": "Bearer canonical-catalog-admin-fixture"},
        )
        self.assertEqual(200, response.status_code)
        return response.json()["capability"]

    async def _get_public_models(self):
        response = await self.client.get(
            "/v1/models",
            headers={"Authorization": "Bearer canonical-catalog-api-key-fixture"},
        )
        self.assertEqual(200, response.status_code)
        return response.json()["data"]

    async def _get_test_models(self):
        capability = await self._issue_test_capability()
        response = await self.client.get(
            "/api/test/models",
            headers={"X-Flow2API-Test-Capability": capability},
        )
        self.assertEqual(200, response.status_code)
        return response.json()["data"]

    async def test_v1_models_exposes_unique_public_ids_for_explicit_video_capabilities(self):
        public_models = await self._get_public_models()
        public_ids = [entry["id"] for entry in public_models]
        self.assertEqual(len(public_ids), len(set(public_ids)))
        for entry in public_models:
            if entry["model_type"] == "video":
                self.assertEqual(entry["capability_id"], entry["id"])

    async def test_public_and_admin_test_catalogs_share_all_visible_mappings_without_hidden_entries(self):
        public_models = await self._get_public_models()
        test_models = await self._get_test_models()
        self.assertEqual(EXPECTED_CAPABILITY_IDS, set(_by_capability(public_models)))
        self.assertEqual(EXPECTED_CAPABILITY_IDS, set(_by_capability(test_models)))
        self.assertEqual(16, len(public_models))
        self.assertEqual(16, len(test_models))

        def visible_mapping_contract(entries):
            return {
                (
                    entry["capability_id"],
                    mapping["parameters"]["aspect_ratio"],
                    mapping["parameters"].get("duration_seconds")
                    or mapping["parameters"].get("resolution"),
                    mapping["model_id"],
                    mapping["validation_status"],
                )
                for entry in entries
                for mapping in entry["compatibility_map"]
                if mapping["validation_status"] in {
                    "validated",
                    "membership_required",
                }
            }

        self.assertEqual(
            visible_mapping_contract(public_models),
            visible_mapping_contract(test_models),
        )
        for entries in (public_models, test_models):
            self.assertFalse(
                any(
                    mapping["validation_status"] == "hidden"
                    for entry in entries
                    for mapping in entry["compatibility_map"]
                )
            )

        for omni in (
            _by_capability(public_models)["omni-flash"],
            _by_capability(test_models)["omni-flash"],
        ):
            self.assertEqual("10", omni["default_parameters"]["duration_seconds"])
            self.assertEqual(
                {"8", "10"},
                _option_values(omni, "duration_seconds"),
            )
            self.assertEqual(
                {
                    ("16:9", "8", "omni"),
                    ("9:16", "8", "omni_portrait"),
                    ("16:9", "10", "omni_10s"),
                    ("9:16", "10", "omni_portrait_10s"),
                },
                {
                    (
                        mapping["parameters"]["aspect_ratio"],
                        mapping["parameters"]["duration_seconds"],
                        mapping["model_id"],
                    )
                    for mapping in omni["compatibility_map"]
                },
            )


if __name__ == "__main__":
    unittest.main()
