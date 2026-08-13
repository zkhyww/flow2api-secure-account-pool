"""Compatibility video task registry."""

import asyncio
import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable, Dict, Optional


PUBLIC_VIDEO_STATUSES = {
    "queued",
    "in_progress",
    "completed",
    "failed",
    "cancelled",
}


class VideoTaskCapacityError(RuntimeError):
    pass


class VideoTaskIdempotencyConflict(RuntimeError):
    pass


@dataclass
class CompatVideoTask:
    id: str
    model: str
    status: str
    progress: int
    created_at: int
    completed_at: Optional[int]
    expires_at: int
    size: Optional[str]
    seconds: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    filename: Optional[str] = None


class CompatVideoTaskRegistry:
    def __init__(
        self,
        *,
        ttl_seconds: int = 7200,
        capacity: int = 256,
        clock: Callable[[], float] = time.time,
    ):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.capacity = max(1, int(capacity))
        self._clock = clock
        self._tasks: Dict[str, CompatVideoTask] = {}
        self._idempotency: Dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()
        self._active_expiry_hook: Optional[Callable[[str], None]] = None

    def set_active_expiry_hook(
        self,
        hook: Optional[Callable[[str], None]],
    ) -> None:
        self._active_expiry_hook = hook

    def _remove_task_unlocked(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)
        stale_digests = [
            digest
            for digest, (_fingerprint, indexed_task_id) in self._idempotency.items()
            if indexed_task_id == task_id
        ]
        for digest in stale_digests:
            self._idempotency.pop(digest, None)

    def _cleanup_expired_unlocked(self) -> None:
        now = int(self._clock())
        for task_id in list(self._tasks):
            task = self._tasks[task_id]
            if task.expires_at > now:
                continue
            if task.status not in {"completed", "failed", "cancelled"}:
                task.status = "failed"
                task.progress = 100
                task.error_code = "task_timeout"
                task.error_message = "Video task timed out"
                task.completed_at = now
                task.expires_at = now + self.ttl_seconds
                stale_digests = [
                    digest
                    for digest, (_fingerprint, indexed_task_id) in self._idempotency.items()
                    if indexed_task_id == task_id
                ]
                for digest in stale_digests:
                    self._idempotency.pop(digest, None)
                if self._active_expiry_hook is not None:
                    self._active_expiry_hook(task_id)
                continue
            self._remove_task_unlocked(task_id)

    def _ensure_capacity_unlocked(self) -> None:
        self._cleanup_expired_unlocked()
        while len(self._tasks) >= self.capacity:
            terminal_tasks = [
                task
                for task in self._tasks.values()
                if task.status in {"completed", "failed", "cancelled"}
            ]
            if not terminal_tasks:
                raise VideoTaskCapacityError("video_task_capacity_reached")
            oldest = min(
                terminal_tasks,
                key=lambda task: (
                    task.completed_at if task.completed_at is not None else task.created_at,
                    task.created_at,
                    task.id,
                ),
            )
            self._remove_task_unlocked(oldest.id)

    async def create(
        self,
        *,
        model: str,
        size: Optional[str],
        seconds: int,
    ) -> CompatVideoTask:
        async with self._lock:
            self._ensure_capacity_unlocked()
            now = int(self._clock())
            task = CompatVideoTask(
                id=f"video_{uuid.uuid4().hex}",
                model=model,
                status="queued",
                progress=0,
                created_at=now,
                completed_at=None,
                expires_at=now + self.ttl_seconds,
                size=size,
                seconds=int(seconds),
            )
            self._tasks[task.id] = task
            return replace(task)

    async def create_idempotent(
        self,
        *,
        model: str,
        size: Optional[str],
        seconds: int,
        idempotency_digest: str,
        request_fingerprint: str,
    ) -> tuple[CompatVideoTask, bool]:
        async with self._lock:
            self._cleanup_expired_unlocked()
            existing = self._idempotency.get(idempotency_digest)
            if existing is not None:
                existing_fingerprint, task_id = existing
                if existing_fingerprint != request_fingerprint:
                    raise VideoTaskIdempotencyConflict("idempotency_conflict")
                task = self._tasks.get(task_id)
                if task is not None:
                    return replace(task), True
                self._idempotency.pop(idempotency_digest, None)

            self._ensure_capacity_unlocked()
            now = int(self._clock())
            task = CompatVideoTask(
                id=f"video_{uuid.uuid4().hex}",
                model=model,
                status="queued",
                progress=0,
                created_at=now,
                completed_at=None,
                expires_at=now + self.ttl_seconds,
                size=size,
                seconds=int(seconds),
            )
            self._tasks[task.id] = task
            self._idempotency[idempotency_digest] = (
                request_fingerprint,
                task.id,
            )
            return replace(task), False

    async def get(self, task_id: str) -> Optional[CompatVideoTask]:
        async with self._lock:
            self._cleanup_expired_unlocked()
            task = self._tasks.get(task_id)
            return replace(task) if task is not None else None

    async def update(
        self,
        task_id: str,
        *,
        status: Optional[str] = None,
        progress: Optional[int] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Optional[CompatVideoTask]:
        async with self._lock:
            self._cleanup_expired_unlocked()
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status in {"completed", "failed", "cancelled"}:
                return replace(task)
            if status is not None:
                if status not in PUBLIC_VIDEO_STATUSES:
                    raise ValueError("invalid_video_task_status")
                task.status = status
            if progress is not None:
                task.progress = max(0, min(100, int(progress)))
            task.error_code = error_code
            task.error_message = error_message
            if filename is not None:
                task.filename = filename
            if task.status in {"completed", "failed", "cancelled"}:
                completed_at = int(self._clock())
                task.completed_at = completed_at
                task.expires_at = completed_at + self.ttl_seconds
            return replace(task)
