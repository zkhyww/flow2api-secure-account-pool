import unittest
from html.parser import HTMLParser
from pathlib import Path


class _CaptchaMethodParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_captcha_method = False
        self.option_values = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "select" and attributes.get("id") == "cfgCaptchaMethod":
            self.in_captcha_method = True
        elif tag == "option" and self.in_captcha_method:
            self.option_values.append(attributes.get("value"))

    def handle_endtag(self, tag):
        if tag == "select" and self.in_captcha_method:
            self.in_captcha_method = False


class ManageExtensionCaptchaOptionTests(unittest.TestCase):
    def test_captcha_method_selector_exposes_extension_mode(self):
        manage_html = Path("static/manage.html").read_text(encoding="utf-8")
        parser = _CaptchaMethodParser()
        parser.feed(manage_html)

        self.assertIn("extension", parser.option_values)


if __name__ == "__main__":
    unittest.main()
