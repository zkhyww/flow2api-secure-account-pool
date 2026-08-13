import socket
import tempfile
import unittest
from unittest.mock import patch

from src.services.file_cache import FileCache


class _FakeResponse:
    status_code = 200
    content = b"synthetic-cached-video"


class _FakeSession:
    init_kwargs = []
    get_kwargs = []

    def __init__(self, *args, **kwargs):
        type(self).init_kwargs.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        type(self).get_kwargs.append(kwargs)
        callback = kwargs.get("content_callback")
        if callback is not None:
            callback(_FakeResponse.content)
        return _FakeResponse()


class YingceFileCachePrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_download_does_not_log_source_url(self):
        source_url = "https://flow-content.google/synthetic-private-media.mp4"
        messages = []
        _FakeSession.init_kwargs = []
        _FakeSession.get_kwargs = []
        public_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.97", 443))
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = FileCache(cache_dir=temp_dir)
            with patch("socket.getaddrinfo", return_value=public_dns):
                with patch("src.services.file_cache.AsyncSession", _FakeSession):
                    with patch(
                        "src.services.file_cache.debug_logger.log_info",
                        side_effect=messages.append,
                    ):
                        filename = await cache.download_and_cache(
                            source_url,
                            "video",
                            log_source_url=False,
                        )

            self.assertTrue((cache.cache_dir / filename).is_file())

        self.assertEqual(1, len(_FakeSession.init_kwargs))
        self.assertIn("curl_options", _FakeSession.init_kwargs[0])
        self.assertEqual(1, len(_FakeSession.get_kwargs))
        self.assertFalse(_FakeSession.get_kwargs[0]["allow_redirects"])
        serialized = "\n".join(str(message) for message in messages)
        self.assertNotIn(source_url, serialized)
        self.assertNotIn("Downloading file from:", serialized)


if __name__ == "__main__":
    unittest.main()
