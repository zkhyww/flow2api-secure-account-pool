import importlib
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


MODULE_NAME = "scripts.real_unattended_soak"
REPORT_FIELDS = {
    "stage",
    "status",
    "started_at",
    "finished_at",
    "planned_count",
    "completed_count",
    "failed_count",
    "image_count",
    "video_count",
    "error_class_counts",
    "has_media_count",
    "latency_seconds_min",
    "latency_seconds_max",
    "latency_seconds_avg",
    "service_alive",
    "browser_final_zero",
    "browser_process_count",
    "rss_start",
    "rss_peak",
    "rss_end",
    "account_resources_final",
}
ERROR_CLASSES = {
    "rate_limited",
    "authentication_failed",
    "membership_required",
    "captcha_failed",
    "upstream_error",
    "timeout",
    "transport_error",
    "http_error",
    "unknown",
}
VIDEO_SMOKE_FIELDS = {
    "stage",
    "status",
    "error_class",
    "has_media",
    "duration_seconds",
    "create_http",
    "poll_http",
    "content_http",
    "media_bytes",
}


class _FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += max(0.0, float(seconds))


class _VideoResponse:
    def __init__(self, status_code, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content

    def json(self):
        return self._payload


class _ScriptedVideoClient:
    def __init__(self, actions, calls):
        self.actions = list(actions)
        self.calls = calls
        self.closed = False

    def _run(self, method, path, kwargs):
        self.calls.append((method, path, kwargs))
        expected_method, expected_path, outcome = self.actions.pop(0)
        if expected_method != method or expected_path != path:
            raise AssertionError((method, path))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def post(self, path, **kwargs):
        return self._run("POST", path, kwargs)

    def get(self, path, **kwargs):
        return self._run("GET", path, kwargs)

    def close(self):
        self.closed = True


class _ScriptedClientFactory:
    def __init__(self, client_actions):
        self.client_actions = list(client_actions)
        self.calls = []
        self.clients = []

    def __call__(self, _api_key):
        client = _ScriptedVideoClient(self.client_actions.pop(0), self.calls)
        self.clients.append(client)
        return client


class RealUnattendedSoakContractTests(unittest.TestCase):
    def _load_harness(self):
        try:
            module_spec = importlib.util.find_spec(MODULE_NAME)
        except ModuleNotFoundError:
            module_spec = None
        self.assertIsNotNone(
            module_spec,
            "missing real unattended soak harness contract",
        )
        return importlib.import_module(MODULE_NAME)

    def test_cli_contract_uses_only_environment_api_key_and_low_frequency_defaults(self):
        harness = self._load_harness()
        parser = harness.build_parser()
        defaults = parser.parse_args([])

        self.assertGreater(defaults.duration_hours, 0)
        self.assertGreaterEqual(defaults.interval_seconds, 300)
        self.assertGreater(defaults.video_every, 1)
        self.assertGreaterEqual(defaults.idle_wait_seconds, 60)
        self.assertTrue(defaults.output)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--api-key", "forbidden"])

        self.assertEqual(
            "environment-only-secret",
            harness.require_api_key(
                {"FLOW2API_SOAK_API_KEY": "environment-only-secret"}
            ),
        )
        with self.assertRaises(harness.ConfigurationError):
            harness.require_api_key({})

    def test_video_smoke_cli_exposes_single_video_kind(self):
        harness = self._load_harness()
        arguments = harness.build_parser().parse_args(["--kind", "video"])
        self.assertEqual("video", arguments.kind)

    def test_video_smoke_default_client_disables_environment_proxies(self):
        harness = self._load_harness()
        sentinel = object()
        with patch.object(httpx, "Client", return_value=sentinel) as client_ctor:
            client = harness._new_video_client("synthetic-key")
        self.assertIs(sentinel, client)
        self.assertFalse(client_ctor.call_args.kwargs["trust_env"])

    def test_scheduler_runs_one_request_per_round_without_retry_and_alternates_video(self):
        harness = self._load_harness()
        output = Path(self._testMethodName + ".json")
        self.addCleanup(output.unlink, missing_ok=True)
        clock = _FakeClock()
        attempts = []

        def run_attempt(kind):
            attempts.append(kind)
            ordinal = len(attempts)
            if ordinal == 2:
                return harness.AttemptResult(
                    completed=False,
                    has_media=False,
                    error_class="rate_limited",
                    latency_seconds=2.0,
                    service_alive=True,
                )
            return harness.AttemptResult(
                completed=True,
                has_media=True,
                error_class=None,
                latency_seconds=float(ordinal),
                service_alive=True,
            )

        def snapshot():
            final_idle = clock.value >= 8.0
            return harness.ProcessSnapshot(
                service_alive=True,
                rss_bytes=100 + int(clock.value),
                browser_process_count=0 if final_idle else 1,
            )

        config = harness.SoakConfig(
            duration_hours=4.0 / 3600.0,
            interval_seconds=1.0,
            video_every=3,
            idle_wait_seconds=5.0,
            output=output,
        )
        report = harness.run_soak(
            config,
            attempt_runner=run_attempt,
            process_snapshot=snapshot,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertEqual(["image", "image", "video", "image"], attempts)
        self.assertEqual(REPORT_FIELDS, set(report))
        self.assertEqual(4, report["planned_count"])
        self.assertEqual(3, report["completed_count"])
        self.assertEqual(1, report["failed_count"])
        self.assertEqual(3, report["image_count"])
        self.assertEqual(1, report["video_count"])
        self.assertEqual(3, report["has_media_count"])
        self.assertEqual({"rate_limited": 1}, report["error_class_counts"])
        self.assertEqual("completed_with_failures", report["status"])
        self.assertTrue(report["service_alive"])
        self.assertTrue(report["browser_final_zero"])
        self.assertEqual(0, report["browser_process_count"])
        self.assertEqual("unknown", report["account_resources_final"])
        self.assertEqual(1.0, report["latency_seconds_min"])
        self.assertEqual(4.0, report["latency_seconds_max"])
        self.assertTrue(math.isclose(2.5, report["latency_seconds_avg"]))
        self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))

    def test_report_allowlist_and_error_taxonomy_reject_every_extra_key(self):
        harness = self._load_harness()
        report = harness.empty_report(planned_count=1)

        self.assertEqual(REPORT_FIELDS, set(report))
        harness.validate_report(report)

        report_with_extra = dict(report, task_id="forbidden")
        with self.assertRaises(ValueError):
            harness.validate_report(report_with_extra)

        report_with_bad_error = dict(report)
        report_with_bad_error["error_class_counts"] = {"private_upstream_code": 1}
        with self.assertRaises(ValueError):
            harness.validate_report(report_with_bad_error)

        self.assertEqual(ERROR_CLASSES, harness.ERROR_CLASSES)
        serialized = json.dumps(report, ensure_ascii=False).lower()
        for forbidden in (
            "cookie",
            "profile",
            "email",
            "http://",
            "https://",
            "prompt",
            "response_body",
            "task_id",
            "idempotency",
            "api_key",
            "account_id",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_atomic_report_writer_replaces_destination_without_temp_residue(self):
        harness = self._load_harness()
        output_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        output = output_dir / "progress.json"
        output.write_text("stale", encoding="utf-8")
        report = harness.empty_report(planned_count=2)

        harness.atomic_write_report(output, report)

        self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))
        self.assertEqual([output], list(output_dir.iterdir()))

    def test_video_smoke_poll_disconnect_reuses_same_task_without_second_post(self):
        harness = self._load_harness()
        task = "synthetic-task"
        factory = _ScriptedClientFactory([
            [
                ("POST", "/v1/videos", _VideoResponse(200, {"id": task})),
                ("GET", f"/v1/videos/{task}", _VideoResponse(200, {"status": "in_progress"})),
                ("GET", f"/v1/videos/{task}", httpx.ReadError("synthetic disconnect")),
            ],
            [
                ("GET", f"/v1/videos/{task}", _VideoResponse(200, {"status": "completed"})),
                ("GET", f"/v1/videos/{task}/content", _VideoResponse(200, content=b"media")),
            ],
        ])
        clock = _FakeClock()
        result = harness.run_yingce_video_smoke(
            "synthetic-key",
            client_factory=factory,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            idempotency_key_factory=lambda: "synthetic-idempotency",
            deadline_seconds=30.0,
            poll_interval_seconds=1.0,
        )

        self.assertEqual(VIDEO_SMOKE_FIELDS, set(result))
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["has_media"])
        self.assertEqual(5, result["media_bytes"])
        self.assertEqual(1, sum(method == "POST" for method, _path, _kwargs in factory.calls))
        serialized = json.dumps(result)
        self.assertNotIn(task, serialized)
        self.assertNotIn("synthetic-idempotency", serialized)
        self.assertNotIn("synthetic-key", serialized)
        self.assertNotIn(harness.VIDEO_CANARY, serialized)
        self.assertNotIn("http://", serialized)
        self.assertNotIn("synthetic disconnect", serialized)

    def test_video_smoke_create_response_loss_retries_only_same_key_and_payload(self):
        harness = self._load_harness()
        task = "synthetic-recovered-task"
        factory = _ScriptedClientFactory([
            [("POST", "/v1/videos", httpx.ReadError("synthetic create loss"))],
            [
                ("POST", "/v1/videos", _VideoResponse(200, {"id": task})),
                ("GET", f"/v1/videos/{task}", _VideoResponse(200, {"status": "completed"})),
                ("GET", f"/v1/videos/{task}/content", _VideoResponse(200, content=b"ok")),
            ],
        ])
        clock = _FakeClock()
        result = harness.run_yingce_video_smoke(
            "synthetic-key",
            client_factory=factory,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            idempotency_key_factory=lambda: "stable-key",
            deadline_seconds=30.0,
            poll_interval_seconds=1.0,
        )

        posts = [(path, kwargs) for method, path, kwargs in factory.calls if method == "POST"]
        self.assertEqual(2, len(posts))
        self.assertEqual(posts[0][0], posts[1][0])
        self.assertEqual(posts[0][1]["data"], posts[1][1]["data"])
        self.assertEqual(posts[0][1]["headers"], posts[1][1]["headers"])
        self.assertEqual("completed", result["status"])
        serialized = json.dumps(result)
        self.assertNotIn(task, serialized)
        self.assertNotIn("stable-key", serialized)
        self.assertNotIn("synthetic create loss", serialized)

    def test_video_smoke_content_disconnect_retries_get_only(self):
        harness = self._load_harness()
        task = "synthetic-content-task"
        factory = _ScriptedClientFactory([
            [
                ("POST", "/v1/videos", _VideoResponse(200, {"id": task})),
                ("GET", f"/v1/videos/{task}", _VideoResponse(200, {"status": "completed"})),
                ("GET", f"/v1/videos/{task}/content", httpx.ReadError("synthetic content loss")),
            ],
            [("GET", f"/v1/videos/{task}/content", _VideoResponse(200, content=b"video"))],
        ])
        clock = _FakeClock()
        result = harness.run_yingce_video_smoke(
            "synthetic-key",
            client_factory=factory,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            idempotency_key_factory=lambda: "content-key",
            deadline_seconds=30.0,
            poll_interval_seconds=1.0,
        )

        self.assertEqual("completed", result["status"])
        self.assertEqual(5, result["media_bytes"])
        self.assertEqual(1, sum(method == "POST" for method, _path, _kwargs in factory.calls))
        self.assertNotIn("synthetic content loss", json.dumps(result))

    def test_video_smoke_poll_recovery_is_bounded(self):
        harness = self._load_harness()
        task = "synthetic-bounded-task"
        factory = _ScriptedClientFactory([
            [("POST", "/v1/videos", _VideoResponse(200, {"id": task})), ("GET", f"/v1/videos/{task}", httpx.ReadError("first"))],
            [("GET", f"/v1/videos/{task}", httpx.ReadError("second"))],
        ])
        clock = _FakeClock()
        result = harness.run_yingce_video_smoke(
            "synthetic-key",
            client_factory=factory,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            idempotency_key_factory=lambda: "bounded-key",
            deadline_seconds=5.0,
            poll_interval_seconds=1.0,
            max_poll_attempts=2,
        )
        self.assertEqual("failed", result["status"])
        self.assertEqual("client_http_error", result["error_class"])
        self.assertLessEqual(clock.value, 5.0)
        self.assertEqual(1, sum(call[0] == "POST" for call in factory.calls))
        self.assertEqual(2, sum(call[0] == "GET" for call in factory.calls))

    def test_models_and_bounded_response_signals_match_current_compatibility_contract(self):
        harness = self._load_harness()

        self.assertEqual(
            "gemini-3.1-flash-image-landscape",
            harness.IMAGE_MODEL,
        )
        self.assertEqual("omni_10s", harness.VIDEO_MODEL)
        self.assertEqual(
            (True, None),
            harness.classify_response_signals(
                200,
                {"media_marker"},
            ),
        )
        cases = (
            (429, set(), "rate_limited"),
            (401, set(), "authentication_failed"),
            (403, {"membership_marker"}, "membership_required"),
            (403, {"captcha_marker"}, "captcha_failed"),
            (502, {"upstream_marker"}, "upstream_error"),
            (400, set(), "http_error"),
            (200, set(), "unknown"),
        )
        for status_code, signals, expected in cases:
            with self.subTest(status_code=status_code, expected=expected):
                self.assertEqual(
                    (False, expected),
                    harness.classify_response_signals(status_code, signals),
                )


if __name__ == "__main__":
    unittest.main()
