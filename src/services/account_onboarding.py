"""Short-lived, credential-free browser account onboarding state."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from ..core.logger import debug_logger


PUBLIC_ONBOARDING_FIELDS = {
    "session_id",
    "stage",
    "status",
    "started_at",
    "expires_at",
    "account_count_before",
    "account_count_after",
    "error_class",
}


@dataclass
class PublicOnboardingState:
    session_id: str
    stage: str
    status: str
    started_at: float
    expires_at: float
    account_count_before: int
    account_count_after: int
    error_class: Optional[str] = None

    def to_public_dict(self) -> Dict[str, Any]:
        payload = {
            "session_id": self.session_id,
            "stage": self.stage,
            "status": self.status,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "account_count_before": self.account_count_before,
            "account_count_after": self.account_count_after,
            "error_class": self.error_class,
        }
        return {field: payload[field] for field in PUBLIC_ONBOARDING_FIELDS}


class AccountOnboardingService:
    """Own one isolated visible-browser onboarding session at a time."""

    TERMINAL_STATUSES = {"success", "updated", "failed", "timeout"}

    def __init__(
        self,
        *,
        account_counter: Callable[[], Awaitable[int]],
        browser_launcher: Callable[[str], Awaitable[Any]],
        ttl_seconds: float = 300,
        poll_interval_seconds: float = 1,
        clock: Callable[[], float] = time.time,
    ):
        self._account_counter = account_counter
        self._browser_launcher = browser_launcher
        self._ttl_seconds = max(0.01, float(ttl_seconds))
        self._poll_interval_seconds = max(0.001, float(poll_interval_seconds))
        self._clock = clock
        self._lock = asyncio.Lock()
        self._states: Dict[str, PublicOnboardingState] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._active_session_id: Optional[str] = None

    async def start(self) -> PublicOnboardingState:
        async with self._lock:
            if self._active_session_id:
                active = self._states.get(self._active_session_id)
                if active and active.status not in self.TERMINAL_STATUSES:
                    return active

            now = self._clock()
            before = max(0, int(await self._account_counter()))
            import secrets

            session_id = f"onboard-{secrets.token_urlsafe(18)}"
            state = PublicOnboardingState(
                session_id=session_id,
                stage="waiting_browser",
                status="running",
                started_at=now,
                expires_at=now + self._ttl_seconds,
                account_count_before=before,
                account_count_after=before,
            )
            self._states[session_id] = state
            self._active_session_id = session_id
            self._tasks[session_id] = asyncio.create_task(self._run(session_id))
            return state

    async def status(self, session_id: str) -> PublicOnboardingState:
        async with self._lock:
            state = self._states.get(str(session_id or ""))
            if state is None:
                raise KeyError("onboarding session not found")
            return state

    async def finish(self, session_id: str, outcome: str) -> PublicOnboardingState:
        normalized = str(outcome or "failed").strip().lower()
        status = normalized if normalized in self.TERMINAL_STATUSES else "failed"
        await self._finish(session_id, status=status, error_class=None if status in {"success", "updated"} else status)
        return await self.status(session_id)

    async def _run(self, session_id: str) -> None:
        try:
            async with self._lock:
                state = self._states[session_id]
                remaining_ttl = max(0.0, state.expires_at - self._clock())
                state.stage = "waiting_login"
            if remaining_ttl <= 0:
                await self._finish(session_id, status="timeout", error_class="timeout")
                return
            try:
                async with asyncio.timeout(remaining_ttl):
                    outcome = str(await self._browser_launcher(session_id) or "").strip().lower()
            except TimeoutError:
                await self._finish(session_id, status="timeout", error_class="timeout")
                return

            if outcome not in {"success", "updated"}:
                await self._finish(session_id, status="failed", error_class="failed")
                return

            current_count = max(0, int(await self._account_counter()))
            async with self._lock:
                state = self._states[session_id]
                state.stage = "importing"
                state.account_count_after = current_count
            await self._finish(session_id, status=outcome, error_class=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            debug_logger.log_warning("[AccountOnboarding] browser onboarding failed")
            await self._finish(
                session_id,
                status="failed",
                error_class="failed",
            )

    async def _finish(self, session_id: str, *, status: str, error_class: Optional[str]) -> None:
        task = None
        async with self._lock:
            state = self._states.get(session_id)
            if state is None:
                raise KeyError("onboarding session not found")
            if state.status not in self.TERMINAL_STATUSES:
                state.status = status
                state.stage = status
                state.error_class = error_class
            if self._active_session_id == session_id:
                self._active_session_id = None
            task = self._tasks.pop(session_id, None)

        current_task = asyncio.current_task()
        if task is not None and task is not current_task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
