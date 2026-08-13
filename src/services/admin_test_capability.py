"""Short-lived capabilities bound to live HttpOnly admin sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable


@dataclass(frozen=True)
class _CapabilityRecord:
    capability_digest: str
    admin_session_digest: str
    expires_at: float


class AdminTestCapabilityService:
    def __init__(self, *, ttl_seconds: float = 300, clock: Callable[[], float] = time.time):
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._clock = clock
        self._lock = asyncio.Lock()
        self._records: Dict[str, _CapabilityRecord] = {}

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

    async def issue(self, admin_session: str) -> str:
        normalized_session = str(admin_session or "").strip()
        if not normalized_session:
            raise PermissionError("missing admin session")
        capability = f"test-{secrets.token_urlsafe(32)}"
        digest = self._digest(capability)
        async with self._lock:
            self._records[digest] = _CapabilityRecord(
                capability_digest=digest,
                admin_session_digest=self._digest(normalized_session),
                expires_at=self._clock() + self._ttl_seconds,
            )
        return capability

    async def verify(self, capability: str, active_admin_sessions: Iterable[str]) -> bool:
        candidate = self._digest(capability)
        async with self._lock:
            record = next(
                (
                    item
                    for digest, item in self._records.items()
                    if hmac.compare_digest(digest, candidate)
                ),
                None,
            )
            if record is None:
                return False
            if self._clock() >= record.expires_at:
                self._records.pop(record.capability_digest, None)
                return False
            active_digests = [self._digest(value) for value in active_admin_sessions]
            return any(
                hmac.compare_digest(record.admin_session_digest, digest)
                for digest in active_digests
            )


_service = AdminTestCapabilityService()


def get_admin_test_capability_service() -> AdminTestCapabilityService:
    return _service
