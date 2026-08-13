import importlib
import importlib.util
import json
import unittest


QA_STATUS_FIELDS = {
    "model",
    "account_id",
    "stage",
    "status",
    "error_class",
    "has_media",
    "duration",
    "attempt_count",
    "delivery_mode",
}

QA_POOL_COUNT_FIELDS = {
    "account_records",
    "active_accounts",
    "inactive_accounts",
    "quota_reservations",
    "image_inflight",
    "video_inflight",
    "browser_reservations",
    "browser_inflight",
}


class LocalQaStatusContractTests(unittest.TestCase):
    def test_status_builder_keeps_only_the_approved_fields_and_aggregate_counts(self):
        module_name = "src.core.local_qa_status"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "local QA status allowlist contract is missing",
        )
        module = importlib.import_module(module_name)

        payload = module.build_local_qa_status(
            {
                "model": "fixture-model",
                "token_id": 2,
                "stage": "completed",
                "status": "succeeded",
                "error_class": "",
                "has_media": True,
                "duration": 8.0,
                "attempt_count": 1,
                "delivery_mode": "cache",
                "email": "private-email",
                "cookie": "private-cookie",
                "token": "private-token",
                "url": "https://private.example/media",
                "prompt": "private-prompt",
                "mediaGenerationId": "private-media-id",
                "response_body": "private-response",
            },
            pool_counts={
                "account_records": 3,
                "active_accounts": 2,
                "inactive_accounts": 1,
                "quota_reservations": 0,
                "image_inflight": 0,
                "video_inflight": 0,
                "browser_reservations": 0,
                "browser_inflight": 0,
                "email": "private-pool-email",
                "url": "https://private.example/pool",
            },
        )

        self.assertEqual(QA_STATUS_FIELDS | {"pool_counts"}, set(payload))
        self.assertEqual(QA_POOL_COUNT_FIELDS, set(payload["pool_counts"]))
        self.assertEqual(2, payload["account_id"])
        self.assertEqual(8.0, payload["duration"])
        self.assertEqual(1, payload["attempt_count"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for private_value in (
            "private-email",
            "private-cookie",
            "private-token",
            "private.example",
            "private-prompt",
            "private-media-id",
            "private-response",
            "private-pool-email",
        ):
            self.assertNotIn(private_value, serialized)


if __name__ == "__main__":
    unittest.main()
