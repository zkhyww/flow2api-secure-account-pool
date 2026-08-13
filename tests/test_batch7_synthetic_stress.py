import importlib
import importlib.util
import json
import unittest
from pathlib import Path


MODULE_NAME = "scripts.synthetic_account_pool_stress"
REPORT_PATH = Path("docs/validation/2026-08-13-synthetic-account-pool-stress-validation.json")
METRIC_FIELDS = {
    "scenario",
    "account_count",
    "worker_limit",
    "operation_count",
    "success_count",
    "failure_count",
    "queued_count",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "throughput_ops_s",
    "peak_memory_bytes",
    "max_live_workers",
    "leak_count",
    "duplicate_submit_count",
    "error_attribution_accuracy",
}


class SyntheticAccountPoolStressContractTests(unittest.IsolatedAsyncioTestCase):
    def _load_harness(self):
        try:
            module_spec = importlib.util.find_spec(MODULE_NAME)
        except ModuleNotFoundError:
            module_spec = None
        self.assertIsNotNone(
            module_spec,
            "missing offline synthetic account-pool stress harness",
        )
        return importlib.import_module(MODULE_NAME)

    async def test_offline_suite_covers_scale_workers_recovery_and_release_paths(self):
        harness = self._load_harness()
        records = await harness.run_stress_suite(sample_count=3)

        self.assertTrue(records)
        self.assertTrue(all(set(record) == METRIC_FIELDS for record in records))
        self.assertEqual(
            {0, 1, 200, 500},
            {
                record["account_count"]
                for record in records
                if record["scenario"] in {
                    "account_pagination_db",
                    "account_pagination_api",
                    "dense_pack",
                }
            },
        )
        self.assertEqual(
            {3, 5, 10},
            {
                record["worker_limit"]
                for record in records
                if record["scenario"] == "browser_lifecycle"
            },
        )
        self.assertTrue(
            all(
                record["queued_count"] >= 1
                for record in records
                if record["scenario"] == "browser_lifecycle"
            )
        )
        self.assertTrue(
            all(
                record["max_live_workers"] == record["worker_limit"]
                for record in records
                if record["scenario"] == "browser_lifecycle"
            )
        )
        self.assertTrue(
            all(
                record["success_count"] == record["operation_count"]
                for record in records
                if record["scenario"] in {
                    "account_pagination_db",
                    "account_pagination_api",
                }
            )
        )
        self.assertTrue(
            any(record["scenario"] == "idempotency_restart_recovery" for record in records)
        )
        self.assertTrue(
            any(record["scenario"] == "rate_limit_cooldown" for record in records)
        )
        self.assertTrue(
            any(record["scenario"] == "release_paths" for record in records)
        )
        self.assertTrue(all(record["failure_count"] == 0 for record in records))
        self.assertTrue(all(record["leak_count"] == 0 for record in records))
        self.assertTrue(all(record["duplicate_submit_count"] == 0 for record in records))
        self.assertTrue(
            all(record["error_attribution_accuracy"] == 1.0 for record in records)
        )

    async def test_metrics_are_aggregate_bounded_and_internally_consistent(self):
        harness = self._load_harness()
        records = await harness.run_stress_suite(sample_count=2)

        for record in records:
            self.assertGreaterEqual(record["operation_count"], 1)
            self.assertGreaterEqual(record["success_count"], 0)
            self.assertGreaterEqual(record["queued_count"], 0)
            self.assertGreaterEqual(record["peak_memory_bytes"], 0)
            self.assertGreater(record["throughput_ops_s"], 0)
            self.assertLessEqual(record["p50_ms"], record["p95_ms"])
            self.assertLessEqual(record["p95_ms"], record["p99_ms"])
            self.assertLessEqual(
                record["success_count"] + record["failure_count"],
                record["operation_count"],
            )

        serialized = json.dumps(records, ensure_ascii=False).lower()
        for forbidden in (
            "cookie",
            "profile",
            "email",
            "http://",
            "https://",
            "prompt",
            "media",
            "response_body",
            "token_id",
            "api_key",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_persisted_quantitative_report_uses_the_same_strict_allowlist(self):
        self.assertTrue(
            REPORT_PATH.exists(),
            "missing quantified synthetic stress report",
        )
        records = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        self.assertTrue(records)
        self.assertTrue(all(set(record) == METRIC_FIELDS for record in records))
        self.assertTrue(all(record["failure_count"] == 0 for record in records))
        self.assertTrue(all(record["leak_count"] == 0 for record in records))
        self.assertTrue(all(record["duplicate_submit_count"] == 0 for record in records))


if __name__ == "__main__":
    unittest.main()
