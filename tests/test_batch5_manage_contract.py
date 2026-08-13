import re
import unittest
from pathlib import Path


class Batch5ManageContractTests(unittest.TestCase):
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

    def test_account_loader_requests_an_explicit_page_and_consumes_the_envelope(self):
        source = self._function_source("loadTokens", "formatExpiry")
        self.assertTrue(source, "manage page has no token loader")

        has_page_parameter = bool(
            re.search(r"/api/tokens[^`'\"]*(?:page|offset)=", source)
            or (
                "URLSearchParams" in source
                and ("page" in source or "offset" in source)
            )
        )
        self.assertTrue(has_page_parameter, "token loader still fetches the unbounded list")
        self.assertRegex(source, r"(?:page_size|limit)")
        self.assertRegex(source, r"\.items\b")
        self.assertRegex(source, r"\.total\b")
        self.assertRegex(source, r"\.has_next\b")

    def test_manage_page_exposes_a_next_page_or_lazy_load_control(self):
        has_control = bool(
            re.search(
                r'id=["\'](?:tokenNextPage|loadMoreTokens|nextTokenPage)["\']',
                self.html,
            )
            or re.search(r'data-token-pagination=["\']next["\']', self.html)
        )
        has_handler = bool(
            re.search(r"(?:loadMoreTokens|loadNextTokenPage|nextTokenPage)\s*=", self.html)
            or re.search(r"function\s+(?:loadMoreTokens|loadNextTokenPage|nextTokenPage)\b", self.html)
        )

        self.assertTrue(has_control, "manage page has no deterministic next-page control")
        self.assertTrue(has_handler, "pagination control is not wired to a page loader")
        self.assertRegex(self.html, r"\.has_next\b")
        self.assertRegex(self.html, r"\.total\b")

    def test_account_rows_render_the_batch5_status_summary(self):
        source = self._function_source("renderTokens", "refreshTokenCredits")
        self.assertTrue(source, "manage page has no account renderer")

        required_fields = {
            "display_name",
            "auth_status",
            "credits_available",
            "image_learned_limit",
            "video_learned_limit",
            "image_inflight",
            "video_inflight",
            "image_cooldown_reason",
            "video_cooldown_reason",
            "browser_in_use",
            "browser_worker_index",
        }
        missing = sorted(field for field in required_fields if field not in source)
        self.assertEqual([], missing, f"account renderer omits Batch 5 fields: {missing}")

    def test_captcha_configuration_never_logs_config_or_secret_inputs(self):
        load_source = self._function_source("loadCaptchaConfig", "saveCaptchaConfig")
        save_source = self._function_source("saveCaptchaConfig", "loadPluginConfig")
        self.assertTrue(load_source, "manage page has no captcha config loader")
        self.assertTrue(save_source, "manage page has no captcha config saver")
        self.assertNotIn(
            "console.log",
            load_source,
            "captcha loader must not log the returned configuration object",
        )
        self.assertNotIn(
            "console.log",
            save_source,
            "captcha saver must not log API keys or proxy URLs",
        )

    def test_list_and_edit_paths_do_not_expect_credentials_from_the_page_payload(self):
        list_source = self._function_source("loadTokens", "formatExpiry")
        render_source = self._function_source("renderTokens", "refreshTokenCredits")
        edit_source = self._function_source("openEditModal", "closeEditModal")
        account_payload_consumers = "\n".join((list_source, render_source, edit_source))

        forbidden_property_patterns = {
            "st": r"\.st\b",
            "at": r"\.at\b",
            "token": r"\.token\b",
            "google_cookies": r"\.google_cookies\b",
            "login_password": r"\.login_password\b",
            "captcha_token": r"\.captcha_token\b",
            "captcha_proxy_url": r"\.captcha_proxy_url\b",
            "proxy_url": r"\.proxy_url\b",
            "prompt": r"\.prompt\b",
            "media_url": r"\.media_url\b",
            "response_body": r"\.response_body\b",
        }
        for field, pattern in forbidden_property_patterns.items():
            with self.subTest(forbidden_field=field):
                self.assertIsNone(
                    re.search(pattern, account_payload_consumers),
                    f"manage account payload still consumes forbidden field {field}",
                )

        self.assertNotIn(
            "allTokens.find",
            edit_source,
            "edit form must not recover credentials from the paginated list payload",
        )


if __name__ == "__main__":
    unittest.main()
