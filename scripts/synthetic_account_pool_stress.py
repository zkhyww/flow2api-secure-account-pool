"""Offline synthetic stress and recovery measurements for the existing account pool."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
import time
import tracemalloc
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, Mock, patch

from src.api import admin
from src.core.account_tiers import PAYGATE_TIER_NOT_PAID
from src.core.config import config
from src.core.database import Database
from src.core.logger import debug_logger
from src.core.models import Task, Token
from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    _PersonalBrowserPoolService,
)
from src.services.concurrency_manager import ConcurrencyManager
from src.services.generation_handler import GenerationHandler
from src.services.load_balancer import LoadBalancer


METRIC_FIELDS = frozenset(
    {
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
)


@dataclass
class _SyntheticAccount:
    id: int
    image_concurrency: int = -1
    video_concurrency: int = -1
    image_enabled: bool = True
    video_enabled: bool = True
    is_active: bool = True
    user_paygate_tier: str = PAYGATE_TIER_NOT_PAID
    at: Any = None
    at_expires: Any = None
    credits: int = 100
    extension_route_key: Any = None
    ban_reason: Any = None
    email: str = ""


class _SyntheticAccountManager:
    def __init__(self, accounts: list[_SyntheticAccount]):
        self.accounts = accounts
        self.db = None

    async def get_active_tokens(self):
        return list(self.accounts)

    def needs_at_refresh(self, _account):
        return False

    async def ensure_valid_token(self, account):
        return account


class _SyntheticBrowser:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _BrowserSnapshot:
    async def get_observability_snapshot(self, account_ids):
        return {
            "overview": {
                "status": "ok",
                "error_class": None,
                "configured_workers": 0,
                "max_workers": 10,
                "created_workers": 0,
                "live_workers": 0,
                "total_reservations": 0,
                "total_inflight": 0,
                "total_capacity": 0,
                "occupied_slots": 0,
            },
            "accounts": {int(account_id): {} for account_id in account_ids},
        }


class _RecoveryAccountManager:
    def __init__(self, account):
        self.account = account

    async def ensure_valid_token(self, account):
        return account

    async def ensure_project_exists(self, _account_id):
        return "synthetic-project"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(max(0.0, float(value)) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _metric(
    *,
    scenario: str,
    account_count: int,
    worker_limit: int,
    operation_count: int,
    success_count: int,
    failure_count: int,
    queued_count: int,
    latencies_ms: list[float],
    elapsed_seconds: float,
    peak_memory_bytes: int,
    max_live_workers: int = 0,
    leak_count: int = 0,
    duplicate_submit_count: int = 0,
    error_attribution_accuracy: float = 1.0,
) -> dict[str, Any]:
    operation_count = max(1, int(operation_count))
    elapsed_seconds = max(float(elapsed_seconds), 0.000001)
    record = {
        "scenario": str(scenario),
        "account_count": max(0, int(account_count)),
        "worker_limit": max(0, int(worker_limit)),
        "operation_count": operation_count,
        "success_count": max(0, int(success_count)),
        "failure_count": max(0, int(failure_count)),
        "queued_count": max(0, int(queued_count)),
        "p50_ms": round(_percentile(latencies_ms, 0.50), 3),
        "p95_ms": round(_percentile(latencies_ms, 0.95), 3),
        "p99_ms": round(_percentile(latencies_ms, 0.99), 3),
        "throughput_ops_s": round(operation_count / elapsed_seconds, 3),
        "peak_memory_bytes": max(0, int(peak_memory_bytes)),
        "max_live_workers": max(0, int(max_live_workers)),
        "leak_count": max(0, int(leak_count)),
        "duplicate_submit_count": max(0, int(duplicate_submit_count)),
        "error_attribution_accuracy": round(
            min(1.0, max(0.0, float(error_attribution_accuracy))),
            6,
        ),
    }
    if set(record) != METRIC_FIELDS:
        raise RuntimeError("synthetic metric allowlist mismatch")
    return record


async def _timed_call(
    operation: Callable[[], Awaitable[Any]],
    latencies_ms: list[float],
):
    started = time.perf_counter()
    try:
        return await operation()
    finally:
        latencies_ms.append((time.perf_counter() - started) * 1000.0)


async def _make_database(account_count: int):
    temp_dir = tempfile.TemporaryDirectory()
    database = Database(str(Path(temp_dir.name) / "synthetic-stress.db"))
    await database.init_db()
    if account_count:
        rows = [
            (
                account_id,
                f"synthetic-placeholder-{account_id:04d}",
                "",
                f"synthetic-{account_id:04d}",
                1,
                account_id % 11,
                1,
                1,
                "2026-01-01 00:00:00",
                "",
                "",
            )
            for account_id in range(1, account_count + 1)
        ]
        async with database._connect(write=True) as connection:
            await connection.executemany(
                """
                INSERT INTO tokens (
                    id, st, email, name, is_active, credits,
                    image_enabled, video_enabled, created_at,
                    google_cookies, login_password
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await connection.commit()
    return temp_dir, database


async def run_account_pagination_scenario(
    account_count: int,
    *,
    via_api: bool,
    sample_count: int,
    page_size: int = 25,
) -> dict[str, Any]:
    temp_dir, database = await _make_database(account_count)
    latencies: list[float] = []
    success_count = 0
    failure_count = 0
    operation_count = 0
    original_db = admin.db
    original_concurrency = admin.concurrency_manager
    tracemalloc.start()
    started = time.perf_counter()
    try:
        if via_api:
            admin.db = database
            admin.concurrency_manager = ConcurrencyManager()
        browser_snapshot = _BrowserSnapshot()
        browser_patch = patch.object(
            BrowserCaptchaService,
            "get_instance",
            new=AsyncMock(return_value=browser_snapshot),
        )
        with browser_patch:
            for _ in range(max(1, int(sample_count))):
                offset = 0
                page_number = 1
                while True:
                    if via_api:
                        payload = await _timed_call(
                            lambda page_number=page_number: admin.get_tokens(
                                page=page_number,
                                page_size=page_size,
                                token="",
                            ),
                            latencies,
                        )
                        items = list(payload.get("items") or [])
                        total = int(payload.get("total") or 0)
                        has_next = bool(payload.get("has_next"))
                        effective_limit = int(payload.get("page_size") or 0)
                    else:
                        payload = await _timed_call(
                            lambda offset=offset: database.get_tokens_page_with_stats(
                                limit=page_size,
                                offset=offset,
                            ),
                            latencies,
                        )
                        items = list(payload.get("items") or [])
                        total = int(payload.get("total") or 0)
                        has_next = bool(payload.get("has_next"))
                        effective_limit = int(payload.get("limit") or 0)

                    operation_count += 1
                    expected_has_next = offset + len(items) < account_count
                    valid = (
                        total == account_count
                        and effective_limit == page_size
                        and len(items) <= page_size
                        and has_next == expected_has_next
                    )
                    if valid:
                        success_count += 1
                    else:
                        failure_count += 1
                    if not has_next:
                        break
                    offset += effective_limit
                    page_number += 1
        _, peak_memory_bytes = tracemalloc.get_traced_memory()
    finally:
        elapsed_seconds = time.perf_counter() - started
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        admin.db = original_db
        admin.concurrency_manager = original_concurrency
        temp_dir.cleanup()

    return _metric(
        scenario="account_pagination_api" if via_api else "account_pagination_db",
        account_count=account_count,
        worker_limit=0,
        operation_count=operation_count,
        success_count=success_count,
        failure_count=failure_count,
        queued_count=0,
        latencies_ms=latencies,
        elapsed_seconds=elapsed_seconds,
        peak_memory_bytes=peak_memory_bytes,
    )


async def run_dense_pack_scenario(account_count: int) -> dict[str, Any]:
    accounts = [_SyntheticAccount(account_id) for account_id in range(1, account_count + 1)]
    concurrency = ConcurrencyManager()
    await concurrency.initialize(accounts)
    balancer = LoadBalancer(_SyntheticAccountManager(accounts), concurrency)
    operation_count = 12 if account_count <= 1 else 30
    selected_ids: list[int] = []
    latencies: list[float] = []
    success_count = 0
    failure_count = 0

    original_mode = config.call_logic_mode
    original_captcha = config.captcha_method
    config.set_call_logic_mode("default")
    config.set_captcha_method("yescaptcha")
    tracemalloc.start()
    started = time.perf_counter()
    try:
        for _ in range(operation_count):
            selected = await _timed_call(
                lambda: balancer.select_token(
                    for_image_generation=True,
                    reserve=True,
                    track_pending=True,
                ),
                latencies,
            )
            if account_count == 0:
                valid = selected is None
            elif selected is None:
                valid = len(selected_ids) >= account_count * 3
            else:
                selected_ids.append(int(selected.id))
                valid = True
            success_count += int(valid)
            failure_count += int(not valid)

        if len(selected_ids) >= 3 and selected_ids[:3] != [1, 1, 1]:
            success_count -= 1
            failure_count += 1

        for account_id in selected_ids:
            await concurrency.release_image(account_id)
            await balancer.release_pending(
                account_id,
                for_image_generation=True,
            )
        _, peak_memory_bytes = tracemalloc.get_traced_memory()
    finally:
        elapsed_seconds = time.perf_counter() - started
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        config.set_call_logic_mode(original_mode)
        config.set_captcha_method(original_captcha)

    leak_count = 0
    for account in accounts:
        leak_count += await concurrency.get_image_inflight(account.id)
        leak_count += await balancer._get_pending_count(account.id, True, False)

    return _metric(
        scenario="dense_pack",
        account_count=account_count,
        worker_limit=0,
        operation_count=operation_count,
        success_count=success_count,
        failure_count=failure_count,
        queued_count=max(0, operation_count - len(selected_ids)) if account_count else 0,
        latencies_ms=latencies,
        elapsed_seconds=elapsed_seconds,
        peak_memory_bytes=peak_memory_bytes,
        leak_count=leak_count,
    )


async def _make_browser_pool(worker_limit: int) -> _PersonalBrowserPoolService:
    pool = _PersonalBrowserPoolService()
    pool._ensure_idle_worker_reaper = AsyncMock()
    pool._is_token_pool_enabled = Mock(return_value=False)
    with patch.object(
        BrowserCaptchaService,
        "_resolve_configured_browser_count",
        return_value=worker_limit,
    ), patch.object(
        pool,
        "_resolve_worker_resident_tabs",
        return_value=1,
    ):
        await pool._ensure_workers()
    pool._ensure_workers = AsyncMock()
    return pool


def _install_blocking_fake_runtime(
    pool: _PersonalBrowserPoolService,
    *,
    release_event: asyncio.Event,
    entries: list[int],
):
    for worker_index, worker in enumerate(pool._workers):
        async def fake_get_token(
            self,
            _project_id,
            action="IMAGE_GENERATION",
            token_id=None,
            *,
            return_slot_id=False,
            _worker_index=worker_index,
        ):
            if not self._initialized or self.browser is None or self.browser.stopped:
                self._initialized = True
                self.browser = _SyntheticBrowser()
            entries.append(_worker_index)
            await release_event.wait()
            result = "synthetic-result"
            slot_id = f"synthetic-slot-{_worker_index + 1}"
            return (result, slot_id) if return_slot_id else result

        worker.get_token = types.MethodType(fake_get_token, worker)


async def _shutdown_all_fake_runtimes(pool: _PersonalBrowserPoolService) -> int:
    shutdown_count = 0
    for worker in pool._workers:
        if worker.browser is None:
            continue
        worker._runtime_last_active_at = time.time() - 600

        async def fake_shutdown(*_args, _worker=worker, **_kwargs):
            _worker.browser = None
            _worker._initialized = False

        worker._shutdown_browser_runtime = AsyncMock(side_effect=fake_shutdown)
        shutdown_count += int(
            await worker.shutdown_idle_runtime_if_needed(idle_ttl_seconds=60)
        )
    return shutdown_count


async def _close_fake_pool(pool: _PersonalBrowserPoolService):
    for worker in list(pool._workers):
        worker.browser = None
        worker._initialized = False
        worker._resident_tabs.clear()
    await pool.close()


async def run_browser_lifecycle_scenario(worker_limit: int) -> dict[str, Any]:
    pool = await _make_browser_pool(worker_limit)
    release_event = asyncio.Event()
    entries: list[int] = []
    _install_blocking_fake_runtime(pool, release_event=release_event, entries=entries)
    latencies: list[float] = []
    operation_count = worker_limit + 2
    failure_count = 0
    queued_count = 0
    max_live_workers = 0
    tasks: list[asyncio.Task] = []
    tracemalloc.start()
    started = time.perf_counter()
    try:
        if any(worker.browser is not None for worker in pool._workers):
            failure_count += 1

        for ordinal in range(worker_limit):
            tasks.append(
                asyncio.create_task(
                    _timed_call(
                        lambda ordinal=ordinal: pool.get_token(
                            f"synthetic-project-{ordinal}",
                            token_id=ordinal + 1,
                        ),
                        latencies,
                    )
                )
            )

        deadline = asyncio.get_running_loop().time() + 1.0
        while len(entries) < worker_limit and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        max_live_workers = max(
            max_live_workers,
            sum(worker.browser is not None for worker in pool._workers),
        )
        if len(entries) != worker_limit or max_live_workers != worker_limit:
            failure_count += 1

        overflow = asyncio.create_task(
            _timed_call(
                lambda: pool.get_token(
                    "synthetic-project-overflow",
                    token_id=worker_limit + 1,
                ),
                latencies,
            )
        )
        tasks.append(overflow)
        await asyncio.sleep(0)
        if overflow.done() or len(entries) != worker_limit:
            failure_count += 1
        else:
            queued_count += 1

        release_event.set()
        await asyncio.gather(*tasks)
        if pool._worker_dispatch_reservations:
            failure_count += 1

        shutdown_count = await _shutdown_all_fake_runtimes(pool)
        if shutdown_count != worker_limit:
            failure_count += 1
        if any(worker.browser is not None for worker in pool._workers):
            failure_count += 1

        wake_started = time.perf_counter()
        wake_result = await pool.get_token(
            "synthetic-project-rewake",
            token_id=worker_limit + 2,
        )
        latencies.append((time.perf_counter() - wake_started) * 1000.0)
        if wake_result != "synthetic-result":
            failure_count += 1
        if sum(worker.browser is not None for worker in pool._workers) != 1:
            failure_count += 1

        await _shutdown_all_fake_runtimes(pool)
        _, peak_memory_bytes = tracemalloc.get_traced_memory()
    finally:
        elapsed_seconds = time.perf_counter() - started
        if tracemalloc.is_tracing():
            tracemalloc.stop()

    leak_count = (
        sum(max(0, int(value)) for value in pool._worker_dispatch_reservations.values())
        + sum(worker.browser is not None for worker in pool._workers)
    )
    await _close_fake_pool(pool)
    success_count = operation_count - min(operation_count, failure_count)

    return _metric(
        scenario="browser_lifecycle",
        account_count=worker_limit + 2,
        worker_limit=worker_limit,
        operation_count=operation_count,
        success_count=success_count,
        failure_count=failure_count,
        queued_count=queued_count,
        latencies_ms=latencies,
        elapsed_seconds=elapsed_seconds,
        peak_memory_bytes=peak_memory_bytes,
        max_live_workers=max_live_workers,
        leak_count=leak_count,
    )


async def run_rate_limit_scenario() -> dict[str, Any]:
    now = [100.0]
    account = _SyntheticAccount(1)
    concurrency = ConcurrencyManager(clock=lambda: now[0])
    await concurrency.initialize([account])
    latencies: list[float] = []
    failures = 0
    tracemalloc.start()
    started = time.perf_counter()
    try:
        acquired = await _timed_call(lambda: concurrency.acquire_image(1), latencies)
        await concurrency.release_image(1)
        await concurrency.record_success(1, "image")
        await concurrency.record_rate_limit(1, "image", cooldown_seconds=5)
        cooling = not await _timed_call(lambda: concurrency.can_use_image(1), latencies)
        now[0] += 6
        recovered = await _timed_call(lambda: concurrency.can_use_image(1), latencies)
        failures += int(not acquired)
        failures += int(not cooling)
        failures += int(not recovered)

        attribution_cases = (
            (429, "RATE_LIMITED", "rate_limited"),
            (401, "UNAUTHENTICATED", "authentication"),
            (402, "QUOTA_EXHAUSTED", "quota_exhausted"),
            (500, "UPSTREAM_FAILURE", "upstream_5xx"),
            (400, "CONTENT_POLICY", "content_policy"),
        )
        correct = sum(
            GenerationHandler.classify_failure(
                stage="generation",
                status_code=status,
                error_code=code,
                has_media=False,
            )
            == expected
            for status, code, expected in attribution_cases
        )
        accuracy = correct / len(attribution_cases)
        failures += int(accuracy != 1.0)
        _, peak_memory_bytes = tracemalloc.get_traced_memory()
    finally:
        elapsed_seconds = time.perf_counter() - started
        if tracemalloc.is_tracing():
            tracemalloc.stop()

    leak_count = await concurrency.get_image_inflight(1)
    return _metric(
        scenario="rate_limit_cooldown",
        account_count=1,
        worker_limit=0,
        operation_count=3,
        success_count=3 - min(3, failures),
        failure_count=failures,
        queued_count=1,
        latencies_ms=latencies,
        elapsed_seconds=elapsed_seconds,
        peak_memory_bytes=peak_memory_bytes,
        leak_count=leak_count,
        error_attribution_accuracy=accuracy,
    )


async def run_release_paths_scenario() -> dict[str, Any]:
    pool = await _make_browser_pool(2)
    concurrency = ConcurrencyManager()
    account = _SyntheticAccount(1)
    await concurrency.initialize([account])
    latencies: list[float] = []
    failures = 0
    tracemalloc.start()
    started = time.perf_counter()
    try:
        if not await _timed_call(lambda: concurrency.acquire_image(1), latencies):
            failures += 1
        await concurrency.release_image(1)
        await concurrency.release_image(1)

        entered = asyncio.Event()
        never = asyncio.Event()

        async def blocking_get_token(self, *_args, **_kwargs):
            entered.set()
            await never.wait()

        for worker in pool._workers:
            worker.get_token = types.MethodType(blocking_get_token, worker)

        cancelled = asyncio.create_task(pool.get_token("synthetic-cancel"))
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        cancelled.cancel()
        try:
            await cancelled
            failures += 1
        except asyncio.CancelledError:
            pass
        latencies.append((time.perf_counter() - started) * 1000.0)
        if pool._worker_dispatch_reservations:
            failures += 1

        entered.clear()
        timeout_started = time.perf_counter()
        try:
            await asyncio.wait_for(
                pool.get_token("synthetic-timeout"),
                timeout=0.01,
            )
            failures += 1
        except asyncio.TimeoutError:
            pass
        latencies.append((time.perf_counter() - timeout_started) * 1000.0)
        if pool._worker_dispatch_reservations:
            failures += 1

        async def failing_get_token(self, *_args, **_kwargs):
            raise RuntimeError("synthetic-boundary-failure")

        for worker in pool._workers:
            worker.get_token = types.MethodType(failing_get_token, worker)
        failed_result = await _timed_call(
            lambda: pool.get_token("synthetic-exception"),
            latencies,
        )
        if failed_result is not None:
            failures += 1

        await pool._release_worker_reservation(0)
        await pool._release_worker_reservation(0)
        if pool._worker_dispatch_reservations:
            failures += 1
        _, peak_memory_bytes = tracemalloc.get_traced_memory()
    finally:
        elapsed_seconds = time.perf_counter() - started
        if tracemalloc.is_tracing():
            tracemalloc.stop()

    leak_count = (
        await concurrency.get_image_inflight(1)
        + sum(max(0, int(value)) for value in pool._worker_dispatch_reservations.values())
    )
    await _close_fake_pool(pool)
    return _metric(
        scenario="release_paths",
        account_count=1,
        worker_limit=2,
        operation_count=4,
        success_count=4 - min(4, failures),
        failure_count=failures,
        queued_count=2,
        latencies_ms=latencies,
        elapsed_seconds=elapsed_seconds,
        peak_memory_bytes=peak_memory_bytes,
        max_live_workers=0,
        leak_count=leak_count,
    )


async def run_idempotency_recovery_scenario() -> dict[str, Any]:
    temp_dir, database = await _make_database(1)
    latencies: list[float] = []
    submit_count = 0
    submit_lock = asyncio.Lock()
    failures = 0
    operation_count = 53
    tracemalloc.start()
    started = time.perf_counter()
    try:
        async def claim_and_submit():
            nonlocal submit_count
            task, created = await database.get_or_create_idempotent_task(
                idempotency_key="synthetic-same-key",
                task_id="synthetic-idempotent-task",
                token_id=1,
                model="synthetic-model",
            )
            if created:
                async with submit_lock:
                    submit_count += 1
                await database.update_task(task.task_id, status="succeeded")
            return task.task_id

        claimed = await asyncio.gather(
            *(_timed_call(claim_and_submit, latencies) for _ in range(50))
        )
        if len(set(claimed)) != 1 or submit_count != 1:
            failures += 1

        for status in ("accepted", "polling", "unknown"):
            await database.create_task(
                Task(
                    task_id=f"synthetic-recovery-{status}",
                    token_id=1,
                    model="synthetic-video",
                    prompt="",
                    status=status,
                )
            )

        account = await database.get_token(1)
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.db = database
        handler.token_manager = _RecoveryAccountManager(account)
        handler.flow_client = object()
        handler._background_poll_tasks = set()
        recovered: list[str] = []

        async def fake_poll(**kwargs):
            recovered.append(kwargs["task_id"])
            await database.update_task(kwargs["task_id"], status="failed")
            return []

        handler._collect_detached_video_poll = fake_poll
        recovery_started = time.perf_counter()
        await handler.recover_incomplete_tasks()
        latencies.append((time.perf_counter() - recovery_started) * 1000.0)
        await asyncio.sleep(0)

        if set(recovered) != {
            "synthetic-recovery-accepted",
            "synthetic-recovery-polling",
        }:
            failures += 1
        if (await database.get_task("synthetic-recovery-unknown")).status != "unknown":
            failures += 1

        quota_task = Task(
            task_id="synthetic-quota-release",
            token_id=1,
            model="synthetic-model",
            prompt="",
            status="failed",
            quota_state="reserved",
            quota_reserved=1,
        )
        await database.create_task(quota_task)
        release_started = time.perf_counter()
        await database.release_task_quota(quota_task.task_id)
        await database.release_task_quota(quota_task.task_id)
        latencies.append((time.perf_counter() - release_started) * 1000.0)

        async with database._connect() as connection:
            cursor = await connection.execute(
                "SELECT COALESCE(SUM(quota_reserved), 0) FROM tasks"
            )
            quota_reservations = int((await cursor.fetchone())[0] or 0)
        _, peak_memory_bytes = tracemalloc.get_traced_memory()
    finally:
        elapsed_seconds = time.perf_counter() - started
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        temp_dir.cleanup()

    duplicate_submit_count = max(0, submit_count - 1)
    leak_count = quota_reservations + len(handler._background_poll_tasks)
    return _metric(
        scenario="idempotency_restart_recovery",
        account_count=1,
        worker_limit=0,
        operation_count=operation_count,
        success_count=operation_count - min(operation_count, failures),
        failure_count=failures,
        queued_count=49,
        latencies_ms=latencies,
        elapsed_seconds=elapsed_seconds,
        peak_memory_bytes=peak_memory_bytes,
        leak_count=leak_count,
        duplicate_submit_count=duplicate_submit_count,
    )


def validate_metric_records(records: list[dict[str, Any]]) -> None:
    if not records:
        raise ValueError("synthetic stress records are empty")
    for record in records:
        if set(record) != METRIC_FIELDS:
            raise ValueError("synthetic stress record violates the allowlist")
        if int(record["failure_count"]) != 0:
            raise ValueError("synthetic stress functional contract failed")
        if int(record["leak_count"]) != 0:
            raise ValueError("synthetic stress leak contract failed")
        if int(record["duplicate_submit_count"]) != 0:
            raise ValueError("synthetic stress duplicate-submit contract failed")


async def run_stress_suite(sample_count: int = 5) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with patch.object(debug_logger, "log_info", return_value=None), patch.object(
        debug_logger,
        "log_warning",
        return_value=None,
    ), patch.object(debug_logger, "log_error", return_value=None):
        for account_count in (0, 1, 200, 500):
            records.append(
                await run_account_pagination_scenario(
                    account_count,
                    via_api=False,
                    sample_count=sample_count,
                )
            )
            records.append(
                await run_account_pagination_scenario(
                    account_count,
                    via_api=True,
                    sample_count=sample_count,
                )
            )
            records.append(await run_dense_pack_scenario(account_count))
        for worker_limit in (3, 5, 10):
            records.append(await run_browser_lifecycle_scenario(worker_limit))
        records.append(await run_rate_limit_scenario())
        records.append(await run_release_paths_scenario())
        records.append(await run_idempotency_recovery_scenario())
    validate_metric_records(records)
    return records


async def _run_cli(sample_count: int) -> int:
    records = await run_stress_suite(sample_count=sample_count)
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run offline synthetic account-pool stress measurements."
    )
    parser.add_argument("--samples", type=int, default=5)
    arguments = parser.parse_args()
    return asyncio.run(_run_cli(max(1, arguments.samples)))


if __name__ == "__main__":
    raise SystemExit(main())
