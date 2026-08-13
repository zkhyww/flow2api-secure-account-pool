import json
import unittest
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowApiError, FlowClient
from src.services.generation_handler import GenerationHandler


class Batch2FailureClassificationTests(unittest.TestCase):
    def setUp(self):
        self.handler = GenerationHandler.__new__(GenerationHandler)

    def test_structured_failures_map_to_stable_public_error_classes(self):
        cases = (
            (
                {
                    "stage": "media_parse",
                    "status_code": 200,
                    "error_code": "MEDIA_EMPTY",
                    "has_media": False,
                },
                "media_empty",
            ),
            (
                {
                    "stage": "submit",
                    "status_code": 400,
                    "error_code": "CONTENT_POLICY",
                    "has_media": False,
                },
                "content_policy",
            ),
            (
                {
                    "stage": "submit",
                    "status_code": 401,
                    "error_code": "AUTHENTICATION",
                    "has_media": False,
                },
                "authentication",
            ),
            (
                {
                    "stage": "submit",
                    "status_code": 402,
                    "error_code": "QUOTA_EXHAUSTED",
                    "has_media": False,
                },
                "quota_exhausted",
            ),
            (
                {
                    "stage": "submit",
                    "status_code": 429,
                    "error_code": "RATE_LIMITED",
                    "has_media": False,
                },
                "rate_limited",
            ),
            (
                {
                    "stage": "poll",
                    "status_code": 503,
                    "error_code": "UPSTREAM_UNAVAILABLE",
                    "has_media": False,
                },
                "upstream_5xx",
            ),
        )

        for structured_failure, expected in cases:
            with self.subTest(expected=expected):
                actual = self.handler.classify_failure(**structured_failure)
                self.assertEqual(actual, expected)

    def test_flow_exception_strings_and_metadata_map_to_public_classes(self):
        class _BoundaryError(Exception):
            def __init__(self, message, *, status_code=0, error_code=""):
                super().__init__(message)
                self.status_code = status_code
                self.error_code = error_code

        cases = (
            (
                Exception("PUBLIC_ERROR_UNUSUAL_ACTIVITY_CHECK: reCAPTCHA evaluation failed"),
                "recaptcha",
            ),
            (
                Exception("PUBLIC_ERROR_MODEL_ACCESS_DENIED: private upstream detail"),
                "model_access_denied",
            ),
            (
                _BoundaryError("opaque", status_code=503, error_code="UPSTREAM_UNAVAILABLE"),
                "upstream_5xx",
            ),
        )
        for exc, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    expected,
                    self.handler.classify_exception_failure(
                        exc,
                        stage="submit",
                        has_media=False,
                    ),
                )

    def test_public_error_response_uses_stable_class_instead_of_generic_generation_failed_code(self):
        payload = json.loads(
            self.handler._create_error_response("model_access_denied", status_code=403)
        )
        self.assertEqual("model_access_denied", payload["error"]["message"])
        self.assertEqual("model_access_denied", payload["error"]["code"])
        self.assertNotIn("private upstream", json.dumps(payload))


class FlowErrorBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_failure_preserves_status_and_reason_without_exposing_upstream_message(self):
        class _Response:
            status_code = 403
            headers = {}
            text = json.dumps(
                {
                    "error": {
                        "message": "private upstream explanation",
                        "details": [{"reason": "PUBLIC_ERROR_MODEL_ACCESS_DENIED"}],
                    }
                }
            )

            def json(self):
                return json.loads(self.text)

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                return _Response()

        client = FlowClient(proxy_manager=None)
        with patch("src.services.flow_client.AsyncSession", return_value=_Session()), patch(
            "src.services.flow_client.debug_logger.log_error"
        ) as log_error:
            with self.assertRaises(Exception) as raised:
                await client._make_request(
                    method="POST",
                    url="https://example.invalid/private-path?secret=url-secret",
                    json_data={"prompt": "body-secret"},
                    allow_urllib_fallback=False,
                )

        exc = raised.exception
        self.assertEqual(403, getattr(exc, "status_code", None))
        self.assertEqual(
            "PUBLIC_ERROR_MODEL_ACCESS_DENIED",
            getattr(exc, "error_code", None),
        )
        self.assertNotIn("private upstream explanation", str(exc))
        logged = "\n".join(str(call) for call in log_error.call_args_list)
        self.assertNotIn("url-secret", logged)
        self.assertNotIn("body-secret", logged)
        self.assertNotIn("private upstream explanation", logged)
        self.assertIn("status=403", logged)
        self.assertIn("error_code=PUBLIC_ERROR_MODEL_ACCESS_DENIED", logged)

    async def test_video_request_boundary_preserves_structured_flow_error(self):
        client = FlowClient(proxy_manager=None)
        boundary_error = FlowApiError(
            status_code=403,
            error_code="PUBLIC_ERROR_MODEL_ACCESS_DENIED",
        )
        client._make_request = AsyncMock(side_effect=boundary_error)

        with self.assertRaises(FlowApiError) as raised:
            await client._make_video_api_request(
                url="https://example.invalid/video",
                json_data={"prompt": "private"},
                at="private-token",
                timeout=5,
            )

        self.assertIs(boundary_error, raised.exception)
        self.assertEqual(403, raised.exception.status_code)
        self.assertEqual(
            "PUBLIC_ERROR_MODEL_ACCESS_DENIED",
            raised.exception.error_code,
        )


if __name__ == "__main__":
    unittest.main()
