import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

from src.api import admin, routes
from src.core.database import Database

from src.services.extension_pairing import ExtensionPairingService


class ExtensionPairingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_atomically_consumes_pairing_handle_once(self):
        service = ExtensionPairingService(ttl_seconds=30, session_ttl_seconds=60)
        issue = await service.issue(instance_id="profile-fixture")

        results = await asyncio.gather(
            service.exchange(issue.pairing_handle),
            service.exchange(issue.pairing_handle),
            return_exceptions=True,
        )

        successes = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(1, len(successes))
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], PermissionError)
        self.assertNotIn(issue.pairing_handle, repr(service._pairings))
        self.assertNotIn(successes[0].plugin_session, repr(service._sessions))

    async def test_expired_pairing_handle_cannot_be_exchanged(self):
        now = 100.0
        service = ExtensionPairingService(
            ttl_seconds=5,
            session_ttl_seconds=60,
            clock=lambda: now,
        )
        issue = await service.issue(instance_id="profile-fixture")
        now = 106.0

        with self.assertRaises(PermissionError):
            await service.exchange(issue.pairing_handle)

    async def test_plugin_session_can_expire_and_be_revoked(self):
        now = 100.0
        service = ExtensionPairingService(
            ttl_seconds=5,
            session_ttl_seconds=10,
            clock=lambda: now,
        )
        first = await service.issue(instance_id="profile-fixture")
        exchanged = await service.exchange(first.pairing_handle)
        binding = await service.verify_session(exchanged.plugin_session)
        self.assertEqual(first.route_key, binding.route_key)
        self.assertEqual(first.client_label, binding.client_label)

        await service.revoke(exchanged.plugin_session)
        self.assertIsNone(await service.verify_session(exchanged.plugin_session))

        second = await service.issue(instance_id="profile-fixture")
        second_session = await service.exchange(second.pairing_handle)
        now = 111.0
        self.assertIsNone(await service.verify_session(second_session.plugin_session))

    async def test_same_profile_instance_has_stable_server_bound_route_identity(self):
        service = ExtensionPairingService(ttl_seconds=30, session_ttl_seconds=60)
        first = await service.issue(instance_id="profile-fixture")
        second = await service.issue(instance_id="profile-fixture")

        self.assertEqual("yingce-flow2api-worker-v1", first.capability_marker)
        self.assertEqual(first.route_key, second.route_key)
        self.assertEqual(first.client_label, second.client_label)
        self.assertNotEqual(first.pairing_handle, second.pairing_handle)

    async def test_persisted_session_verifies_after_service_recreation_without_storing_secrets(self):
        """A restart must retain only a session digest and its non-secret binding."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(str(Path(temp_dir) / "pairing-test.db"))
            await database.init_db()
            service_a = ExtensionPairingService(storage=database)
            issue = await service_a.issue(instance_id="restart-profile")
            session = await service_a.exchange(issue.pairing_handle)

            service_b = ExtensionPairingService(storage=database)
            binding = await service_b.verify_session(session.plugin_session)

            self.assertIsNotNone(binding)
            self.assertEqual("restart-profile", binding.instance_id)
            self.assertEqual("yingce-flow2api-worker-v1", binding.capability_marker)

            connection = sqlite3.connect(database.db_path)
            try:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(extension_plugin_sessions)")
                }
                rows = list(connection.execute("SELECT * FROM extension_plugin_sessions"))
            finally:
                connection.close()
            self.assertEqual(
                {
                    "session_digest",
                    "public_id",
                    "instance_id",
                    "route_key",
                    "client_label",
                    "capability_marker",
                    "expires_at",
                    "created_at",
                },
                columns,
            )
            self.assertEqual(1, len(rows))
            stored_text = repr(rows)
            self.assertNotIn(session.plugin_session, stored_text)
            self.assertNotIn(issue.pairing_handle, stored_text)

    async def test_persisted_expiry_and_public_revocation_apply_across_service_instances(self):
        """A restarted verifier must enforce expiry and administrator revocation."""
        now = 100.0
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(str(Path(temp_dir) / "pairing-test.db"))
            await database.init_db()
            issuer = ExtensionPairingService(
                storage=database,
                clock=lambda: now,
                session_ttl_seconds=10,
            )
            expired_issue = await issuer.issue(instance_id="expiry-profile")
            expired_session = await issuer.exchange(expired_issue.pairing_handle)
            now = 111.0

            restarted = ExtensionPairingService(storage=database, clock=lambda: now)
            self.assertIsNone(await restarted.verify_session(expired_session.plugin_session))
            connection = sqlite3.connect(database.db_path)
            try:
                expired_count = connection.execute(
                    "SELECT COUNT(*) FROM extension_plugin_sessions"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(0, expired_count)

            now = 200.0
            active_issue = await issuer.issue(instance_id="revocation-profile")
            active_session = await issuer.exchange(active_issue.pairing_handle)
            self.assertTrue(await restarted.revoke_public_id(active_session.plugin_session_id))
            self.assertIsNone(await issuer.verify_session(active_session.plugin_session))
            verifier = ExtensionPairingService(storage=database, clock=lambda: now)
            self.assertIsNone(await verifier.verify_session(active_session.plugin_session))

    async def test_persistent_pairing_storage_is_ready_before_full_database_startup(self):
        """An injected production database must not reject a pairing before normal startup init."""
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(str(Path(temp_dir) / "pairing-test.db"))
            service = ExtensionPairingService(storage=database)
            issue = await service.issue(instance_id="early-pairing-profile")
            session = await service.exchange(issue.pairing_handle)

            restarted = ExtensionPairingService(storage=database)
            self.assertIsNotNone(await restarted.verify_session(session.plugin_session))


class _RejectedWebSocket:
    def __init__(self, plugin_session):
        self.headers = {
            "sec-websocket-protocol": (
                f"flow2api-plugin, flow2api-session.{plugin_session}"
            )
        }
        self.closed_code = None

    async def close(self, code):
        self.closed_code = code


class ExtensionPairingRevocationApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = __import__(
            "src.services.extension_pairing",
            fromlist=["get_extension_pairing_service"],
        ).get_extension_pairing_service()
        admin.active_admin_tokens.add("admin-session-fixture")
        app = FastAPI()
        app.include_router(admin.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        admin.active_admin_tokens.discard("admin-session-fixture")
        await self.client.aclose()

    async def test_admin_can_revoke_public_plugin_session_id_for_http_and_websocket(self):
        issue = await self.service.issue(instance_id="revoke-profile-fixture")
        session = await self.service.exchange(issue.pairing_handle)
        self.assertTrue(session.plugin_session_id)
        self.assertNotEqual(session.plugin_session, session.plugin_session_id)

        unauthorized = await self.client.delete(
            f"/api/admin/extension-sessions/{session.plugin_session_id}"
        )
        self.assertEqual(401, unauthorized.status_code)
        revoked = await self.client.delete(
            f"/api/admin/extension-sessions/{session.plugin_session_id}",
            headers={"Authorization": "Bearer admin-session-fixture"},
        )
        self.assertEqual(200, revoked.status_code)
        self.assertEqual(
            {"success": True, "session_id": session.plugin_session_id},
            revoked.json(),
        )
        self.assertNotIn(session.plugin_session, revoked.text)

        http_denied = await self.client.post(
            "/api/plugin/import-current-account",
            headers={"Authorization": f"Bearer {session.plugin_session}"},
            json={"session_token": "fixture"},
        )
        self.assertEqual(401, http_denied.status_code)

        websocket = _RejectedWebSocket(session.plugin_session)
        await routes.captcha_websocket_endpoint(websocket)
        self.assertEqual(1008, websocket.closed_code)


if __name__ == "__main__":
    unittest.main()
