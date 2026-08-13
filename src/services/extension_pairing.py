"""One-use extension pairing and revocable plugin sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


CAPABILITY_MARKER = "yingce-flow2api-worker-v1"
DEFAULT_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class PairingIssue:
    pairing_handle: str
    instance_id: str
    route_key: str
    client_label: str
    capability_marker: str
    expires_at: float


@dataclass(frozen=True)
class PluginSession:
    plugin_session: str
    plugin_session_id: str
    instance_id: str
    route_key: str
    client_label: str
    capability_marker: str
    expires_at: float


@dataclass(frozen=True)
class PluginSessionBinding:
    instance_id: str
    route_key: str
    client_label: str
    capability_marker: str
    expires_at: float


@dataclass(frozen=True)
class _PairingRecord:
    digest: str
    instance_id: str
    route_key: str
    client_label: str
    expires_at: float


@dataclass(frozen=True)
class _SessionRecord:
    digest: str
    public_id: str
    binding: PluginSessionBinding


class ExtensionPairingService:
    """Store only secret digests; exchange pairing handles atomically."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 120,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        storage: Optional[Any] = None,
    ):
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._session_ttl_seconds = max(1.0, float(session_ttl_seconds))
        self._clock = clock
        self._storage = storage
        self._lock = asyncio.Lock()
        self._pairings: Dict[str, _PairingRecord] = {}
        self._sessions: Dict[str, _SessionRecord] = {}

    @staticmethod
    def _digest(secret: str) -> str:
        return hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _identity(instance_id: str) -> tuple[str, str]:
        short_id = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:12]
        return f"flow-{short_id}", f"chrome-{short_id}"

    @staticmethod
    def _find_digest(records: Dict[str, object], candidate: str) -> Optional[str]:
        for stored_digest in records:
            if hmac.compare_digest(stored_digest, candidate):
                return stored_digest
        return None

    async def issue(self, *, instance_id: str) -> PairingIssue:
        normalized_instance = str(instance_id or "").strip() or f"profile-{secrets.token_urlsafe(12)}"
        route_key, client_label = self._identity(normalized_instance)
        handle = f"pair-{secrets.token_urlsafe(32)}"
        digest = self._digest(handle)
        expires_at = self._clock() + self._ttl_seconds
        record = _PairingRecord(
            digest=digest,
            instance_id=normalized_instance,
            route_key=route_key,
            client_label=client_label,
            expires_at=expires_at,
        )
        async with self._lock:
            self._pairings[digest] = record
        return PairingIssue(
            pairing_handle=handle,
            instance_id=normalized_instance,
            route_key=route_key,
            client_label=client_label,
            capability_marker=CAPABILITY_MARKER,
            expires_at=expires_at,
        )

    async def exchange(self, pairing_handle: str) -> PluginSession:
        candidate = self._digest(pairing_handle)
        async with self._lock:
            stored_digest = self._find_digest(self._pairings, candidate)
            record = self._pairings.pop(stored_digest, None) if stored_digest else None
            if record is None or self._clock() >= record.expires_at:
                raise PermissionError("invalid or expired pairing handle")

            raw_session = f"plugin-{secrets.token_urlsafe(32)}"
            public_id = f"ps-{secrets.token_urlsafe(12)}"
            session_digest = self._digest(raw_session)
            expires_at = self._clock() + self._session_ttl_seconds
            binding = PluginSessionBinding(
                instance_id=record.instance_id,
                route_key=record.route_key,
                client_label=record.client_label,
                capability_marker=CAPABILITY_MARKER,
                expires_at=expires_at,
            )
            self._sessions[session_digest] = _SessionRecord(
                digest=session_digest,
                public_id=public_id,
                binding=binding,
            )

            if self._storage is not None:
                await self._storage.create_extension_plugin_session(
                    session_digest=session_digest,
                    public_id=public_id,
                    instance_id=binding.instance_id,
                    route_key=binding.route_key,
                    client_label=binding.client_label,
                    capability_marker=binding.capability_marker,
                    expires_at=binding.expires_at,
                    created_at=self._clock(),
                )

        return PluginSession(
            plugin_session=raw_session,
            plugin_session_id=public_id,
            instance_id=binding.instance_id,
            route_key=binding.route_key,
            client_label=binding.client_label,
            capability_marker=binding.capability_marker,
            expires_at=binding.expires_at,
        )

    async def verify_session(self, plugin_session: str) -> Optional[PluginSessionBinding]:
        candidate = self._digest(plugin_session)
        async with self._lock:
            stored_digest = self._find_digest(self._sessions, candidate)
            record = self._sessions.get(stored_digest) if stored_digest else None
            if self._storage is not None:
                persisted = await self._storage.get_extension_plugin_session_by_digest(
                    candidate, now=self._clock()
                )
                if persisted is None:
                    self._sessions.pop(stored_digest or candidate, None)
                    return None
                if hmac.compare_digest(str(persisted["session_digest"]), candidate):
                    binding = PluginSessionBinding(
                        instance_id=str(persisted["instance_id"]),
                        route_key=str(persisted["route_key"]),
                        client_label=str(persisted["client_label"]),
                        capability_marker=str(persisted["capability_marker"]),
                        expires_at=float(persisted["expires_at"]),
                    )
                    record = _SessionRecord(
                        digest=candidate,
                        public_id=str(persisted["public_id"]),
                        binding=binding,
                    )
                    self._sessions[candidate] = record
                else:
                    self._sessions.pop(stored_digest or candidate, None)
                    return None
            if record is None:
                return None
            if self._clock() >= record.binding.expires_at:
                self._sessions.pop(stored_digest or candidate, None)
                if self._storage is not None:
                    await self._storage.delete_extension_plugin_session_by_digest(record.digest)
                return None
            return record.binding

    async def revoke(self, plugin_session: str) -> bool:
        candidate = self._digest(plugin_session)
        async with self._lock:
            stored_digest = self._find_digest(self._sessions, candidate)
            if stored_digest is None:
                if self._storage is None:
                    return False
                persisted = await self._storage.get_extension_plugin_session_by_digest(
                    candidate, now=self._clock()
                )
                if persisted is None or not hmac.compare_digest(
                    str(persisted["session_digest"]), candidate
                ):
                    return False
                self._sessions.pop(candidate, None)
                return await self._storage.delete_extension_plugin_session_by_digest(candidate)
            self._sessions.pop(stored_digest, None)
            if self._storage is not None:
                await self._storage.delete_extension_plugin_session_by_digest(stored_digest)
            return True

    async def revoke_public_id(self, plugin_session_id: str) -> bool:
        normalized_id = str(plugin_session_id or "").strip()
        async with self._lock:
            match = next(
                (
                    digest
                    for digest, record in self._sessions.items()
                    if hmac.compare_digest(record.public_id, normalized_id)
                ),
                None,
            )
            if match is None:
                if self._storage is None:
                    return False
                persisted = await self._storage.get_extension_plugin_session_by_public_id(
                    normalized_id, now=self._clock()
                )
                if persisted is None or not hmac.compare_digest(
                    str(persisted["public_id"]), normalized_id
                ):
                    return False
                digest = str(persisted["session_digest"])
                self._sessions.pop(digest, None)
                return await self._storage.delete_extension_plugin_session_by_digest(digest)
            self._sessions.pop(match, None)
            if self._storage is not None:
                await self._storage.delete_extension_plugin_session_by_digest(match)
            return True


_service = ExtensionPairingService()


def get_extension_pairing_service() -> ExtensionPairingService:
    return _service


def configure_extension_pairing_storage(storage: Any) -> ExtensionPairingService:
    """Bind the singleton to the application database during production startup."""
    global _service
    if _service._storage is not storage:
        _service = ExtensionPairingService(storage=storage)
    return _service
