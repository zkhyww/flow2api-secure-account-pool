import re
import unittest
from pathlib import Path


class ManagePageBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("static/manage.html").read_text(encoding="utf-8")

    @classmethod
    def _function_source(cls, name, next_name):
        start_marker = f"{name}="
        end_marker = f",\n        {next_name}="
        start = cls.html.find(start_marker)
        if start < 0:
            return ""
        end = cls.html.find(end_marker, start)
        return cls.html[start:] if end < 0 else cls.html[start:end]

    def test_check_auth_exists_before_dom_bootstrap(self):
        definition = self.html.find("checkAuth=async")
        bootstrap = self.html.find("DOMContentLoaded")
        self.assertGreaterEqual(definition, 0, "manage page must define checkAuth")
        self.assertGreater(bootstrap, definition, "checkAuth must exist before bootstrap uses it")
        source = self._function_source("checkAuth", "loadStats")
        self.assertIn("apiRequest(", source)
        self.assertIn("/api/stats", source)

    def test_account_list_has_loading_error_and_empty_states(self):
        self.assertIn('id="tokenLoadState"', self.html)
        self.assertIn('id="retryTokenLoad"', self.html)
        self.assertIn("正在加载账号", self.html)
        self.assertIn("账号加载失败", self.html)
        self.assertIn("暂无账号", self.html)

        source = self._function_source("loadTokens", "loadNextTokenPage")
        self.assertTrue(source, "manage page has no token loader")
        self.assertRegex(source, r"setTokenListState\(['\"]loading['\"]")
        self.assertRegex(source, r"setTokenListState\(['\"]ready['\"]")
        self.assertRegex(source, r"setTokenListState\(['\"]error['\"]")

    def test_failed_load_does_not_render_zero_account_summary(self):
        source = self._function_source("loadTokens", "loadNextTokenPage")
        catch_match = re.search(r"catch\(e\)\{(?P<body>.*?)\}\s*$", source, re.DOTALL)
        self.assertIsNotNone(catch_match, "loadTokens must handle request failures")
        failure_source = catch_match.group("body")
        self.assertNotIn("tokenTotal=0", failure_source)
        self.assertNotIn("renderTokens()", failure_source)
        self.assertIn("账号加载失败", failure_source)

    def test_retry_control_reloads_tokens(self):
        self.assertRegex(
            self.html,
            r'id=["\']retryTokenLoad["\'][^>]+onclick=["\'](?:loadTokens|refreshTokens)\(\)["\']',
        )


if __name__ == "__main__":
    unittest.main()
