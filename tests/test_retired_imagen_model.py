import unittest

from src.api.routes import _get_openai_model_catalog
from src.core.model_resolver import get_base_model_aliases, resolve_model_name
from src.services.generation_handler import MODEL_CONFIG


class RetiredImagenModelTests(unittest.TestCase):
    def test_retired_imagen_names_resolve_to_live_narwhal_models(self):
        expected = {
            "imagen-4.0-generate-preview": "gemini-3.1-flash-image-landscape",
            "imagen-4.0-generate-preview-landscape": "gemini-3.1-flash-image-landscape",
            "imagen-4.0-generate-preview-portrait": "gemini-3.1-flash-image-portrait",
        }

        for retired_name, live_name in expected.items():
            with self.subTest(retired_name=retired_name):
                resolved = resolve_model_name(
                    retired_name,
                    model_config=MODEL_CONFIG,
                )
                self.assertEqual(live_name, resolved)
                self.assertEqual("NARWHAL", MODEL_CONFIG[resolved]["model_name"])

    def test_model_catalogs_do_not_advertise_retired_imagen(self):
        openai_ids = {item["id"] for item in _get_openai_model_catalog()}

        self.assertFalse(
            any(model_id.startswith("imagen-4.0-generate-preview") for model_id in openai_ids)
        )
        self.assertNotIn("imagen-4.0-generate-preview", get_base_model_aliases())

if __name__ == "__main__":
    unittest.main()
