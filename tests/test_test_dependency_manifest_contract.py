import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _package_names(text: str) -> set[str]:
    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip().casefold()
        if name:
            names.add(name)
    return names


class TestDependencyManifestContractTests(unittest.TestCase):
    def test_test_dependencies_are_separate_from_runtime_dependencies(self):
        runtime_requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        test_requirements_path = REPO_ROOT / "requirements-test.txt"
        self.assertTrue(
            test_requirements_path.is_file(),
            "missing independent requirements-test.txt",
        )
        test_requirements = test_requirements_path.read_text(encoding="utf-8")

        self.assertEqual({"pytest", "pillow"}, _package_names(test_requirements))
        self.assertNotIn("pytest", _package_names(runtime_requirements))
        self.assertNotIn("pillow", _package_names(runtime_requirements))

    def test_runtime_and_test_install_steps_are_documented(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        guide = (REPO_ROOT / "docs" / "USER_GUIDE_ZH.md").read_text(encoding="utf-8")
        for text in (readme, guide):
            self.assertIn("requirements.txt", text)
            self.assertIn("requirements-test.txt", text)


if __name__ == "__main__":
    unittest.main()
