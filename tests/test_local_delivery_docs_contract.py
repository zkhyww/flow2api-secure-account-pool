import importlib.util
import re
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DELIVERY_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "USER_GUIDE_ZH.md",
    REPO_ROOT / "docs" / "FORK_DIFFERENCES_ZH.md",
)
SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"sk-[0-9A-Za-z_-]{20,}"),
    re.compile(r"Bearer\s+(?!<your_api_key>)[0-9A-Za-z._-]{8,}"),
)


class LocalDeliveryDocsContractTests(unittest.TestCase):
    def test_delivery_docs_use_only_approved_api_key_placeholders(self):
        texts = {}
        for path in DELIVERY_DOCS:
            self.assertTrue(path.is_file(), f"missing delivery document: {path.name}")
            texts[path.name] = path.read_text(encoding="utf-8")

        combined = "\n".join(texts.values())
        self.assertNotIn("<api_key>", combined)
        self.assertNotIn("han1234", combined)
        self.assertIn("<your_api_key>", combined)
        for pattern in SECRET_PATTERNS:
            self.assertIsNone(pattern.search(combined))

    def test_local_user_guide_does_not_recommend_api_key_in_url(self):
        guide = (REPO_ROOT / "docs" / "USER_GUIDE_ZH.md").read_text(encoding="utf-8")
        self.assertNotIn("?key=", guide)
        self.assertIn("不要把 API Key 放进 URL", guide)

    def test_readme_links_local_delivery_guides(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/USER_GUIDE_ZH.md", readme)
        self.assertIn("docs/FORK_DIFFERENCES_ZH.md", readme)

    def test_clean_delivery_secret_scanner_is_count_only_and_rejects_runtime_secrets(self):
        scanner_path = REPO_ROOT / "scripts" / "scan_delivery_secrets.py"
        self.assertTrue(scanner_path.is_file(), "missing clean-delivery secret scanner")
        spec = importlib.util.spec_from_file_location("delivery_secret_scanner", scanner_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "README.md").write_text("Bearer <your_api_key>\n", encoding="utf-8")
            (root / "src" / "app.py").write_text("print('safe')\n", encoding="utf-8")

            clean = module.scan_delivery_tree(root)
            self.assertEqual(
                {"files_scanned", "forbidden_path_count", "secret_pattern_count"},
                set(clean),
            )
            self.assertEqual(0, clean["forbidden_path_count"])
            self.assertEqual(0, clean["secret_pattern_count"])

            synthetic_secret = "Bearer " + "fixture-" + ("x" * 24)
            (root / "notes.txt").write_text(synthetic_secret, encoding="utf-8")
            leaked = module.scan_delivery_tree(root)
            self.assertEqual(1, leaked["secret_pattern_count"])

            (root / "config").mkdir()
            (root / "config" / "setting.toml").write_text("opaque-fixture", encoding="utf-8")
            forbidden = module.scan_delivery_tree(root)
            self.assertEqual(1, forbidden["forbidden_path_count"])

    def test_clean_delivery_scanner_allows_only_exact_public_fingerprint_and_explicit_test_fixtures(self):
        scanner_path = REPO_ROOT / "scripts" / "scan_delivery_secrets.py"
        spec = importlib.util.spec_from_file_location("delivery_secret_scanner_allowlist", scanner_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source_text = (REPO_ROOT / "src" / "services" / "flow_client.py").read_text(encoding="utf-8")
        public_matches = module._SECRET_PATTERNS[0].findall(source_text)
        self.assertEqual(1, len(public_matches))
        public_value = public_matches[0]
        synthetic_bearer = "Bear" + "er fixture-" + ("x" * 24)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed_path = root / "src" / "services" / "flow_client.py"
            allowed_path.parent.mkdir(parents=True)
            allowed_path.write_text(public_value, encoding="utf-8")
            fixture_path = root / "tests" / "test_fixture.py"
            fixture_path.parent.mkdir(parents=True)
            fixture_path.write_text(synthetic_bearer, encoding="utf-8")

            allowed = module.scan_delivery_tree(root)
            self.assertEqual(0, allowed["forbidden_path_count"])
            self.assertEqual(0, allowed["secret_pattern_count"])

            other_path = root / "src" / "services" / "other.py"
            other_path.write_text(public_value, encoding="utf-8")
            wrong_path = module.scan_delivery_tree(root)
            self.assertEqual(1, wrong_path["secret_pattern_count"])
            other_path.unlink()

            replacement = "A" if public_value[-1] != "A" else "B"
            allowed_path.write_text(public_value[:-1] + replacement, encoding="utf-8")
            changed_value = module.scan_delivery_tree(root)
            self.assertEqual(1, changed_value["secret_pattern_count"])

    def test_clean_delivery_scanner_still_detects_unknown_credentials_and_runtime_paths(self):
        scanner_path = REPO_ROOT / "scripts" / "scan_delivery_secrets.py"
        spec = importlib.util.spec_from_file_location("delivery_secret_scanner_detection", scanner_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        unknown_google = "AI" + "za" + ("Q" * 35)
        unknown_github = "gh" + "p_" + ("R" * 24)
        unknown_bearer = "Bear" + "er " + ("opaque" * 4)
        private_key_header = "-----BEGIN " + "PRIVATE KEY-----"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "tests"
            source_dir.mkdir()
            (source_dir / "test_unknown_credentials.txt").write_text(
                "\n".join((unknown_google, unknown_github, unknown_bearer, private_key_header)),
                encoding="utf-8",
            )
            detected = module.scan_delivery_tree(root)
            self.assertEqual(4, detected["secret_pattern_count"])

            runtime_dir = root / "data"
            runtime_dir.mkdir()
            (runtime_dir / "runtime.txt").write_text("opaque-runtime-fixture", encoding="utf-8")
            forbidden = module.scan_delivery_tree(root)
            self.assertEqual(1, forbidden["forbidden_path_count"])
            self.assertEqual(4, forbidden["secret_pattern_count"])

    def test_current_source_tree_has_no_unclassified_secret_patterns(self):
        scanner_path = REPO_ROOT / "scripts" / "scan_delivery_secrets.py"
        spec = importlib.util.spec_from_file_location("delivery_secret_scanner_current_tree", scanner_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        current = module.scan_delivery_tree(REPO_ROOT)
        self.assertEqual(0, current["secret_pattern_count"])

    def test_gitignore_excludes_runtime_and_plaintext_secret_artifacts(self):
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        for required in (
            ".env",
            "*.db",
            "*.sqlite",
            "*.log",
            "logs.txt",
            "data",
            "tmp/",
            "browser_data",
            "browser_data_rt",
            "config/setting.toml",
        ):
            with self.subTest(required=required):
                self.assertIn(required, gitignore)


if __name__ == "__main__":
    unittest.main()
