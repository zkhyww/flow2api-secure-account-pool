"""Low-frequency real soak harness with aggregate-only diagnostics."""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Set, Tuple

import httpx


API_KEY_ENV = "FLOW2API_SOAK_API_KEY"
SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 8000
COMPLETIONS_PATH = "/v1/chat/completions"
IMAGE_MODEL = "gemini-3.1-flash-image-landscape"
VIDEO_MODEL = "omni_10s"
VIDEO_COMPAT_MODEL = "omni"
REQUEST_TIMEOUT_SECONDS = 900.0
IMAGE_CANARY = "A simple blue circle centered on a plain white background."
VIDEO_CANARY = "A simple blue circle moves slowly across a plain white background."
VIDEO_CREATE_PATH = "/v1/videos"
VIDEO_SMOKE_STAGE = "yingce_compat_video_smoke"
VIDEO_SMOKE_DEADLINE_SECONDS = 900.0
VIDEO_POLL_INTERVAL_SECONDS = 2.0
VIDEO_MAX_CREATE_ATTEMPTS = 2
VIDEO_MAX_POLL_ATTEMPTS = 300
VIDEO_MAX_CONTENT_ATTEMPTS = 3
VIDEO_CLIENT_TIMEOUT_SECONDS = 60.0
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
_BROWSER_NAMES = {"chrome", "chrome.exe", "msedge", "msedge.exe", "chromium", "chromium.exe"}


class ConfigurationError(ValueError):
    """Raised for a missing or invalid local soak configuration."""


@dataclass(frozen=True)
class SoakConfig:
    duration_hours: float
    interval_seconds: float
    video_every: int
    idle_wait_seconds: float
    output: Path


@dataclass(frozen=True)
class AttemptResult:
    completed: bool
    has_media: bool
    error_class: Optional[str]
    latency_seconds: float
    service_alive: bool


@dataclass(frozen=True)
class ProcessSnapshot:
    service_alive: bool
    rss_bytes: int
    browser_process_count: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a low-frequency aggregate-only Flow2API soak.",
    )
    parser.add_argument("--kind", choices=("soak", "video"), default="soak")
    parser.add_argument("--duration-hours", type=float, default=4.0)
    parser.add_argument("--interval-seconds", type=float, default=900.0)
    parser.add_argument("--video-every", type=int, default=8)
    parser.add_argument("--idle-wait-seconds", type=float, default=80.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/validation/real-unattended-soak-latest.json"),
    )
    return parser


def require_api_key(environment: Mapping[str, str]) -> str:
    value = str(environment.get(API_KEY_ENV) or "").strip()
    if not value:
        raise ConfigurationError("required environment credential is unavailable")
    return value


def _validate_config(config: SoakConfig) -> None:
    if not math.isfinite(config.duration_hours) or config.duration_hours <= 0:
        raise ConfigurationError("duration must be positive")
    if not math.isfinite(config.interval_seconds) or config.interval_seconds <= 0:
        raise ConfigurationError("interval must be positive")
    if config.video_every <= 0:
        raise ConfigurationError("video cadence must be positive")
    if not math.isfinite(config.idle_wait_seconds) or config.idle_wait_seconds < 0:
        raise ConfigurationError("idle wait must not be negative")


def empty_report(planned_count: int) -> dict:
    report = {
        "stage": "initializing",
        "status": "running",
        "started_at": _utc_now(),
        "finished_at": None,
        "planned_count": max(0, int(planned_count)),
        "completed_count": 0,
        "failed_count": 0,
        "image_count": 0,
        "video_count": 0,
        "error_class_counts": {},
        "has_media_count": 0,
        "latency_seconds_min": 0.0,
        "latency_seconds_max": 0.0,
        "latency_seconds_avg": 0.0,
        "service_alive": False,
        "browser_final_zero": False,
        "browser_process_count": 0,
        "rss_start": 0,
        "rss_peak": 0,
        "rss_end": 0,
        "account_resources_final": "unknown",
    }
    validate_report(report)
    return report


def validate_report(report: dict) -> None:
    if set(report) != REPORT_FIELDS:
        raise ValueError("soak report violates the strict allowlist")
    errors = report.get("error_class_counts")
    if not isinstance(errors, dict) or not set(errors).issubset(ERROR_CLASSES):
        raise ValueError("soak report contains a non-public error class")
    if any(not isinstance(count, int) or count < 0 for count in errors.values()):
        raise ValueError("soak report contains an invalid error count")
    if report.get("account_resources_final") != "unknown":
        raise ValueError("account resource state is unavailable without credentials")


def atomic_write_report(output: Path, report: dict) -> None:
    validate_report(report)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(report, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def classify_response_signals(
    status_code: int,
    signals: Set[str],
) -> Tuple[bool, Optional[str]]:
    if 200 <= int(status_code) < 300 and "media_marker" in signals:
        return True, None
    if int(status_code) == 429 or "rate_limit_marker" in signals:
        return False, "rate_limited"
    if int(status_code) == 401 or "authentication_marker" in signals:
        return False, "authentication_failed"
    if int(status_code) == 402 or "membership_marker" in signals:
        return False, "membership_required"
    if "captcha_marker" in signals:
        return False, "captcha_failed"
    if int(status_code) >= 500 or "upstream_marker" in signals:
        return False, "upstream_error"
    if int(status_code) >= 400:
        return False, "http_error"
    return False, "unknown"


def _scan_response(stream) -> Set[str]:
    signals: Set[str] = set()
    tail = ""
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        text = tail + chunk.decode("utf-8", errors="ignore").lower()
        if "![generated image](" in text or "<video" in text:
            signals.add("media_marker")
        if "rate_limited" in text or "too many requests" in text:
            signals.add("rate_limit_marker")
        if "authentication" in text or "unauthorized" in text:
            signals.add("authentication_marker")
        if "membership_required" in text or "membership_tier" in text:
            signals.add("membership_marker")
        if "recaptcha" in text or "captcha_failed" in text:
            signals.add("captcha_marker")
        if "upstream_error" in text or "upstream_5xx" in text or "generation_failed" in text:
            signals.add("upstream_marker")
        tail = text[-128:]
    return signals


def _completion_endpoint() -> str:
    return f"http://{SERVICE_HOST}:{SERVICE_PORT}{COMPLETIONS_PATH}"


def _new_video_client(api_key: str):
    return httpx.Client(
        base_url=f"http://{SERVICE_HOST}:{SERVICE_PORT}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=VIDEO_CLIENT_TIMEOUT_SECONDS,
        trust_env=False,
    )


def _safe_video_error_class(payload: object, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error")
    if not isinstance(error, dict):
        return fallback
    value = str(error.get("code") or "").strip().lower()
    if not value or len(value) > 64:
        return fallback
    if not value[0].isalpha() or any(not (char.isalnum() or char == "_") for char in value):
        return fallback
    return value


def _video_smoke_result(
    *,
    status: str,
    error_class: str,
    has_media: bool,
    duration_seconds: float,
    create_http: int,
    poll_http: int,
    content_http: int,
    media_bytes: int,
) -> dict:
    result = {
        "stage": VIDEO_SMOKE_STAGE,
        "status": status,
        "error_class": error_class,
        "has_media": bool(has_media),
        "duration_seconds": round(max(0.0, float(duration_seconds)), 3),
        "create_http": max(0, int(create_http)),
        "poll_http": max(0, int(poll_http)),
        "content_http": max(0, int(content_http)),
        "media_bytes": max(0, int(media_bytes)),
    }
    if set(result) != VIDEO_SMOKE_FIELDS:
        raise ValueError("video smoke result violates the strict allowlist")
    return result


def run_real_attempt(kind: str, api_key: str) -> AttemptResult:
    model = VIDEO_MODEL if kind == "video" else IMAGE_MODEL
    canary = VIDEO_CANARY if kind == "video" else IMAGE_CANARY
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": canary}],
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        _completion_endpoint(),
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    status_code = 0
    signals: Set[str] = set()
    service_alive = False
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status_code = int(response.status)
            service_alive = True
            signals = _scan_response(response)
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        service_alive = True
        signals = _scan_response(exc)
    except (TimeoutError, socket.timeout):
        return AttemptResult(False, False, "timeout", time.monotonic() - started, True)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        error_class = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "transport_error"
        return AttemptResult(False, False, error_class, time.monotonic() - started, False)
    except OSError:
        return AttemptResult(False, False, "transport_error", time.monotonic() - started, False)

    completed, error_class = classify_response_signals(status_code, signals)
    return AttemptResult(
        completed=completed,
        has_media=completed,
        error_class=error_class,
        latency_seconds=time.monotonic() - started,
        service_alive=service_alive,
    )


def run_yingce_video_smoke(
    api_key: str,
    *,
    client_factory=None,
    monotonic=time.monotonic,
    sleep=time.sleep,
    idempotency_key_factory=None,
    deadline_seconds: float = VIDEO_SMOKE_DEADLINE_SECONDS,
    poll_interval_seconds: float = VIDEO_POLL_INTERVAL_SECONDS,
    max_create_attempts: int = VIDEO_MAX_CREATE_ATTEMPTS,
    max_poll_attempts: int = VIDEO_MAX_POLL_ATTEMPTS,
    max_content_attempts: int = VIDEO_MAX_CONTENT_ATTEMPTS,
) -> dict:
    started = float(monotonic())
    deadline = started + max(0.1, float(deadline_seconds))
    factory = client_factory or _new_video_client
    key_factory = idempotency_key_factory or (lambda: uuid.uuid4().hex)
    idempotency_key = str(key_factory() or "").strip() or uuid.uuid4().hex
    payload = {"model": VIDEO_COMPAT_MODEL, "prompt": VIDEO_CANARY, "seconds": "10"}
    create_http = poll_http = content_http = 0
    task_id = ""

    def finish(status: str, error_class: str, has_media: bool, media_bytes: int = 0) -> dict:
        return _video_smoke_result(
            status=status,
            error_class=error_class,
            has_media=has_media,
            duration_seconds=float(monotonic()) - started,
            create_http=create_http,
            poll_http=poll_http,
            content_http=content_http,
            media_bytes=media_bytes,
        )

    try:
        client = factory(api_key)
    except Exception:
        return finish("failed", "client_http_error", False)

    def done(status: str, error_class: str, has_media: bool, media_bytes: int = 0) -> dict:
        _close_video_client(client)
        return finish(status, error_class, has_media, media_bytes)

    create_limit = max(1, int(max_create_attempts))
    for attempt in range(create_limit):
        if float(monotonic()) >= deadline:
            return done("failed", "client_http_error", False)
        try:
            response = client.post(
                VIDEO_CREATE_PATH,
                data=payload,
                headers={"Idempotency-Key": idempotency_key},
                timeout=max(0.1, min(VIDEO_CLIENT_TIMEOUT_SECONDS, deadline - float(monotonic()))),
            )
        except httpx.HTTPError:
            if attempt + 1 >= create_limit:
                return done("failed", "client_http_error", False)
            if not _sleep_video_retry(deadline=deadline, seconds=1.0, monotonic=monotonic, sleep=sleep):
                return done("failed", "client_http_error", False)
            _close_video_client(client)
            try:
                client = factory(api_key)
            except Exception:
                return finish("failed", "client_http_error", False)
            continue
        create_http = int(getattr(response, "status_code", 0) or 0)
        try:
            body = response.json()
        except (TypeError, ValueError):
            return done("failed", "client_http_error", False)
        if not 200 <= create_http < 300:
            return done(
                "failed",
                _safe_video_error_class(body, "client_http_error"),
                False,
            )
        task_id = str(body.get("id") or "").strip() if isinstance(body, dict) else ""
        if not task_id:
            return done("failed", "client_http_error", False)
        break
    if not task_id:
        return done("failed", "client_http_error", False)

    poll_limit = max(1, int(max_poll_attempts))
    completed = False
    for attempt in range(poll_limit):
        if float(monotonic()) >= deadline:
            return done("failed", "client_http_error", False)
        try:
            response = client.get(
                f"/v1/videos/{task_id}",
                timeout=max(0.1, min(VIDEO_CLIENT_TIMEOUT_SECONDS, deadline - float(monotonic()))),
            )
        except httpx.HTTPError:
            if attempt + 1 >= poll_limit:
                return done("failed", "client_http_error", False)
            if not _sleep_video_retry(
                deadline=deadline,
                seconds=max(0.1, float(poll_interval_seconds)),
                monotonic=monotonic,
                sleep=sleep,
            ):
                return done("failed", "client_http_error", False)
            _close_video_client(client)
            try:
                client = factory(api_key)
            except Exception:
                return finish("failed", "client_http_error", False)
            continue
        poll_http = int(getattr(response, "status_code", 0) or 0)
        try:
            body = response.json()
        except (TypeError, ValueError):
            return done("failed", "client_http_error", False)
        if not 200 <= poll_http < 300:
            return done("failed", _safe_video_error_class(body, "client_http_error"), False)
        task_status = str(body.get("status") or "").strip().lower() if isinstance(body, dict) else ""
        if task_status == "failed":
            return done("failed", _safe_video_error_class(body, "generation_failed"), False)
        if task_status == "completed":
            completed = True
            break
        if task_status not in {"queued", "in_progress"}:
            return done("failed", "client_http_error", False)
        if attempt + 1 >= poll_limit or not _sleep_video_retry(
            deadline=deadline,
            seconds=max(0.1, float(poll_interval_seconds)),
            monotonic=monotonic,
            sleep=sleep,
        ):
            return done("failed", "client_http_error", False)
    if not completed:
        return done("failed", "client_http_error", False)

    content_limit = max(1, int(max_content_attempts))
    for attempt in range(content_limit):
        if float(monotonic()) >= deadline:
            return done("failed", "client_http_error", False)
        try:
            response = client.get(
                f"/v1/videos/{task_id}/content",
                timeout=max(0.1, min(VIDEO_CLIENT_TIMEOUT_SECONDS, deadline - float(monotonic()))),
            )
        except httpx.HTTPError:
            if attempt + 1 >= content_limit:
                return done("failed", "client_http_error", False)
            if not _sleep_video_retry(deadline=deadline, seconds=1.0, monotonic=monotonic, sleep=sleep):
                return done("failed", "client_http_error", False)
            _close_video_client(client)
            try:
                client = factory(api_key)
            except Exception:
                return finish("failed", "client_http_error", False)
            continue
        content_http = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= content_http < 300:
            try:
                body = response.json()
            except (TypeError, ValueError):
                body = {}
            return done("failed", _safe_video_error_class(body, "client_http_error"), False)
        media_bytes = len(bytes(getattr(response, "content", b"") or b""))
        if media_bytes <= 0:
            return done("failed", "client_http_error", False)
        return done("completed", "", True, media_bytes)
    return done("failed", "client_http_error", False)


def _close_video_client(client) -> None:
    try:
        client.close()
    except Exception:
        pass


def _sleep_video_retry(*, deadline, seconds, monotonic, sleep) -> bool:
    remaining = float(deadline) - float(monotonic())
    if remaining <= 0:
        return False
    sleep(min(max(0.0, float(seconds)), remaining))
    return float(monotonic()) < float(deadline)


def _powershell_process_snapshot() -> ProcessSnapshot:
    script = """
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
$processes = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,WorkingSetSize)
[pscustomobject]@{listeners=$listeners;processes=$processes} | ConvertTo-Json -Depth 4 -Compress
"""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=creation_flags,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return ProcessSnapshot(False, 0, 0)
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return ProcessSnapshot(False, 0, 0)

    listeners = payload.get("listeners") or []
    if not isinstance(listeners, list):
        listeners = [listeners]
    roots = {int(pid) for pid in listeners if str(pid).isdigit()}
    processes = payload.get("processes") or []
    if not isinstance(processes, list):
        processes = [processes]
    rows = {}
    for process in processes:
        try:
            pid = int(process.get("ProcessId"))
            rows[pid] = {
                "parent": int(process.get("ParentProcessId") or 0),
                "name": str(process.get("Name") or "").lower(),
                "rss": max(0, int(process.get("WorkingSetSize") or 0)),
            }
        except (AttributeError, TypeError, ValueError):
            continue

    descendants = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, process in rows.items():
            if pid not in descendants and process["parent"] in descendants:
                descendants.add(pid)
                changed = True
    return ProcessSnapshot(
        service_alive=bool(roots),
        rss_bytes=sum(rows[pid]["rss"] for pid in descendants if pid in rows),
        browser_process_count=sum(
            1
            for pid in descendants
            if pid in rows and rows[pid]["name"] in _BROWSER_NAMES
        ),
    )


def process_snapshot() -> ProcessSnapshot:
    if os.name == "nt":
        return _powershell_process_snapshot()
    try:
        with socket.create_connection((SERVICE_HOST, SERVICE_PORT), timeout=1.0):
            return ProcessSnapshot(True, 0, 0)
    except OSError:
        return ProcessSnapshot(False, 0, 0)


def run_soak(
    config: SoakConfig,
    *,
    attempt_runner: Callable[[str], AttemptResult],
    process_snapshot: Callable[[], ProcessSnapshot],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    _validate_config(config)
    duration_seconds = config.duration_hours * 3600.0
    planned_count = max(1, int(math.ceil(duration_seconds / config.interval_seconds)))
    report = empty_report(planned_count)
    latencies = []
    error_counts: Counter[str] = Counter()
    initial = process_snapshot()
    service_alive = initial.service_alive
    report.update(
        stage="running",
        service_alive=service_alive,
        rss_start=initial.rss_bytes,
        rss_peak=initial.rss_bytes,
        rss_end=initial.rss_bytes,
    )
    atomic_write_report(config.output, report)
    schedule_started = monotonic()

    for ordinal in range(1, planned_count + 1):
        due_at = schedule_started + ((ordinal - 1) * config.interval_seconds)
        wait_seconds = due_at - monotonic()
        if wait_seconds > 0:
            sleep(wait_seconds)
        kind = "video" if ordinal % config.video_every == 0 else "image"
        try:
            result = attempt_runner(kind)
        except Exception:
            result = AttemptResult(False, False, "unknown", 0.0, False)

        latency = max(0.0, float(result.latency_seconds))
        latencies.append(latency)
        report[f"{kind}_count"] += 1
        report["completed_count"] += int(result.completed)
        report["failed_count"] += int(not result.completed)
        report["has_media_count"] += int(result.has_media)
        service_alive = service_alive and bool(result.service_alive)
        if not result.completed:
            error_class = result.error_class if result.error_class in ERROR_CLASSES else "unknown"
            error_counts[error_class] += 1

        current = process_snapshot()
        service_alive = service_alive and current.service_alive
        report.update(
            error_class_counts=dict(sorted(error_counts.items())),
            latency_seconds_min=round(min(latencies), 3),
            latency_seconds_max=round(max(latencies), 3),
            latency_seconds_avg=round(sum(latencies) / len(latencies), 3),
            service_alive=service_alive,
            rss_peak=max(report["rss_peak"], current.rss_bytes),
            rss_end=current.rss_bytes,
        )
        atomic_write_report(config.output, report)

    report["stage"] = "idle_wait"
    atomic_write_report(config.output, report)
    if config.idle_wait_seconds:
        sleep(config.idle_wait_seconds)
    final = process_snapshot()
    service_alive = service_alive and final.service_alive
    report.update(
        stage="finished",
        status="completed" if report["failed_count"] == 0 else "completed_with_failures",
        finished_at=_utc_now(),
        service_alive=service_alive,
        browser_final_zero=bool(final.service_alive and final.browser_process_count == 0),
        browser_process_count=max(0, int(final.browser_process_count)),
        rss_peak=max(report["rss_peak"], final.rss_bytes),
        rss_end=max(0, int(final.rss_bytes)),
    )
    atomic_write_report(config.output, report)
    return report


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        api_key = require_api_key(os.environ)
        if arguments.kind == "video":
            result = run_yingce_video_smoke(api_key)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["status"] == "completed" and result["has_media"] else 1
        config = SoakConfig(
            duration_hours=arguments.duration_hours,
            interval_seconds=arguments.interval_seconds,
            video_every=arguments.video_every,
            idle_wait_seconds=arguments.idle_wait_seconds,
            output=arguments.output,
        )
        report = run_soak(
            config,
            attempt_runner=lambda kind: run_real_attempt(kind, api_key),
            process_snapshot=process_snapshot,
        )
    except ConfigurationError:
        print("configuration_error", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
