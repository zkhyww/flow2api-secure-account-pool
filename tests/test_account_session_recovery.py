import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from src.core.config import config
from src.core.database import Database
from src.core.models import Token
from src.services.account_profile_store import AccountProfileStore
from src.services.token_manager import TokenManager


class AccountSessionRecoveryDataLayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._temp_dir.name) / "recovery.db")
        self.db = Database(self.db_path)
        await self.db.init_db()

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    async def _add_token(self, suffix: str) -> Token:
        token = Token(
            st=f"synthetic-st-{suffix}",
            email=f"fixture-{suffix}@example.invalid",
            protocol_mode="protocol",
            auto_refresh_enabled=True,
        )
        token.id = await self.db.add_token(token)
        return token

    async def test_token_auth_recovery_fields_have_safe_defaults(self):
        token = Token(st="synthetic-st-default", email="fixture-default@example.invalid")

        self.assertEqual("", token.account_profile_key)
        self.assertEqual("ok", token.auth_state)
        self.assertEqual(0, token.auth_failure_count)
        self.assertIsNone(token.auth_next_retry_at)
        self.assertEqual("", token.last_auth_error_class)

    async def test_fresh_schema_contains_auth_recovery_columns(self):
        async with aiosqlite.connect(self.db_path) as connection:
            cursor = await connection.execute("PRAGMA table_info(tokens)")
            columns = {row[1]: row for row in await cursor.fetchall()}

        self.assertTrue(
            {
                "account_profile_key",
                "auth_state",
                "auth_failure_count",
                "auth_next_retry_at",
                "last_auth_error_class",
            }.issubset(columns)
        )
        self.assertEqual("''", columns["account_profile_key"][4])
        self.assertEqual("'ok'", columns["auth_state"][4])
        self.assertEqual("0", columns["auth_failure_count"][4])
        self.assertEqual("''", columns["last_auth_error_class"][4])

    async def test_recovery_candidates_preserve_user_enable_intent_and_backoff(self):
        now = datetime.now(timezone.utc)
        ready = await self._add_token("ready")
        disabled = await self._add_token("disabled")
        backing_off = await self._add_token("backoff")
        retry_due = await self._add_token("retry-due")
        reauth = await self._add_token("reauth")

        await self.db.update_token(disabled.id, is_active=False)
        await self.db.update_token_auth_state(
            backing_off.id,
            state="backoff",
            failure_count=1,
            next_retry_at=now + timedelta(minutes=5),
            error_class="network",
        )
        await self.db.update_token_auth_state(
            retry_due.id,
            state="backoff",
            failure_count=2,
            next_retry_at=now - timedelta(seconds=1),
            error_class="network",
        )
        await self.db.update_token_auth_state(
            reauth.id,
            state="reauth_required",
            failure_count=1,
            next_retry_at=None,
            error_class="interactive_verification",
        )

        candidates = await self.db.get_auth_recovery_candidates(now)
        candidate_ids = {token.id for token in candidates}

        self.assertIn(ready.id, candidate_ids)
        self.assertIn(retry_due.id, candidate_ids)
        self.assertNotIn(disabled.id, candidate_ids)
        self.assertNotIn(backing_off.id, candidate_ids)
        self.assertNotIn(reauth.id, candidate_ids)

    async def test_auth_state_update_is_allowlisted_and_does_not_change_is_active(self):
        token = await self._add_token("allowlist")

        await self.db.update_token_auth_state(
            token.id,
            state="refresh_pending",
            failure_count=0,
            next_retry_at=None,
            error_class="",
        )
        refreshed = await self.db.get_token(token.id)
        self.assertTrue(refreshed.is_active)
        self.assertEqual("refresh_pending", refreshed.auth_state)

        with self.assertRaises(ValueError):
            await self.db.update_token_auth_state(
                token.id,
                state="unknown",
                failure_count=0,
                next_retry_at=None,
                error_class="",
            )
        with self.assertRaises(ValueError):
            await self.db.update_token_auth_state(
                token.id,
                state="backoff",
                failure_count=1,
                next_retry_at=datetime.now(timezone.utc),
                error_class="raw upstream response",
            )



class AccountReauthAtomicCommitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._temp_dir.name) / "reauth-atomic.db"))
        await self.db.init_db()

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    async def _add_token(self, suffix: str) -> Token:
        token = Token(
            st=f"synthetic-st-{suffix}",
            email=f"fixture-{suffix}@example.invalid",
            protocol_mode="protocol",
            auto_refresh_enabled=True,
        )
        token.id = await self.db.add_token(token)
        return token

    async def test_reauth_commit_updates_profile_auth_and_enable_state_together(self):
        token = await self._add_token("reauth-commit")
        await self.db.update_token(token.id, is_active=False, account_profile_key="a" * 32)
        await self.db.update_token_auth_state(
            token.id,
            state="reauth_required",
            failure_count=2,
            next_retry_at=None,
            error_class="interactive_verification",
        )

        await self.db.commit_account_reauth(
            token.id,
            st="fixture",
            at="fixture",
            at_expires=None,
            google_cookies="fixture",
            account_profile_key="b" * 32,
        )

        refreshed = await self.db.get_token(token.id)
        self.assertEqual("b" * 32, refreshed.account_profile_key)
        self.assertTrue(refreshed.is_active)
        self.assertEqual("ok", refreshed.auth_state)
        self.assertEqual(0, refreshed.auth_failure_count)
        self.assertIsNone(refreshed.auth_next_retry_at)
        self.assertEqual("", refreshed.last_auth_error_class)

    async def test_reauth_commit_records_successful_st_refresh_timestamp_atomically(self):
        token = await self._add_token("reauth-refresh-clock")
        await self.db.update_token(
            token.id,
            last_st_refresh_at=datetime.now(timezone.utc) - timedelta(hours=3),
            last_st_refresh_result="protocol_refresh_failed",
        )
        started_at = datetime.now(timezone.utc)

        await self.db.commit_account_reauth(
            token.id,
            st="fixture",
            at="fixture",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=24),
            google_cookies="fixture",
            account_profile_key="e" * 32,
        )

        finished_at = datetime.now(timezone.utc)
        refreshed = await self.db.get_token(token.id)
        refresh_at = refreshed.last_st_refresh_at
        self.assertIsNotNone(refresh_at)
        if refresh_at.tzinfo is None:
            refresh_at = refresh_at.replace(tzinfo=timezone.utc)
        self.assertGreaterEqual(refresh_at, started_at - timedelta(seconds=1))
        self.assertLessEqual(refresh_at, finished_at + timedelta(seconds=1))
        self.assertEqual("success", refreshed.last_st_refresh_result)

    async def test_successful_reauth_is_not_due_on_immediate_protocol_refresh_tick(self):
        token = await self._add_token("reauth-no-immediate-refresh")
        await self.db.update_token_refresh_config(
            enabled=True,
            refresh_interval_minutes=120,
        )
        await self.db.commit_account_reauth(
            token.id,
            st="fixture",
            at="fixture",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=24),
            google_cookies="fixture",
            account_profile_key="f" * 32,
        )
        flow_client = SimpleNamespace(
            proxy_manager=None,
            get_request_fingerprint=lambda: {},
            _set_request_fingerprint=lambda _value: None,
        )
        manager = TokenManager(self.db, flow_client)
        manager._refresh_protocol_token = AsyncMock()

        await manager.run_protocol_refresh_once()

        manager._refresh_protocol_token.assert_not_awaited()

    async def test_reauth_commit_rolls_back_profile_reference_when_transaction_fails(self):
        token = await self._add_token("reauth-rollback")
        old_profile_key = "c" * 32
        await self.db.update_token(token.id, is_active=False, account_profile_key=old_profile_key)
        await self.db.update_token_auth_state(
            token.id,
            state="reauth_required",
            failure_count=1,
            next_retry_at=None,
            error_class="interactive_verification",
        )
        async with self.db._connect(write=True) as connection:
            await connection.execute("DROP TABLE token_stats")
            await connection.commit()

        with self.assertRaises(aiosqlite.OperationalError):
            await self.db.commit_account_reauth(
                token.id,
                st="fixture",
                at="fixture",
                at_expires=None,
                google_cookies="fixture",
                account_profile_key="d" * 32,
            )

        refreshed = await self.db.get_token(token.id)
        self.assertEqual(old_profile_key, refreshed.account_profile_key)
        self.assertFalse(refreshed.is_active)
        self.assertEqual("reauth_required", refreshed.auth_state)


class AccountSessionRecoveryTokenManagerTests(AccountSessionRecoveryDataLayerTests):
    def _manager(self, db=None):
        flow_client = SimpleNamespace(
            proxy_manager=None,
            get_request_fingerprint=lambda: {},
            _set_request_fingerprint=lambda _value: None,
        )
        return TokenManager(db or self.db, flow_client)

    async def test_transient_auth_failure_enters_bounded_backoff_without_disabling(self):
        token = await self._add_token("transient")
        manager = self._manager()
        started_at = datetime.now(timezone.utc)

        for _ in range(8):
            await manager._mark_auth_failure(token.id, "network", interactive=False)

        refreshed = await self.db.get_token(token.id)
        self.assertTrue(refreshed.is_active)
        self.assertEqual("backoff", refreshed.auth_state)
        self.assertEqual(8, refreshed.auth_failure_count)
        self.assertEqual("network", refreshed.last_auth_error_class)
        self.assertIsNotNone(refreshed.auth_next_retry_at)
        retry_at = refreshed.auth_next_retry_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        self.assertGreater(retry_at, started_at)
        self.assertLessEqual(retry_at, datetime.now(timezone.utc) + timedelta(minutes=31))

    async def test_interactive_auth_failure_requires_relogin_without_retry_deadline(self):
        token = await self._add_token("interactive")
        manager = self._manager()

        await manager._mark_auth_failure(
            token.id,
            "interactive_verification",
            interactive=True,
        )

        refreshed = await self.db.get_token(token.id)
        self.assertTrue(refreshed.is_active)
        self.assertEqual("reauth_required", refreshed.auth_state)
        self.assertEqual(1, refreshed.auth_failure_count)
        self.assertIsNone(refreshed.auth_next_retry_at)

    async def test_auth_success_clears_failure_state_without_changing_enabled_state(self):
        token = await self._add_token("success")
        manager = self._manager()
        await self.db.update_token_auth_state(
            token.id,
            state="backoff",
            failure_count=3,
            next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            error_class="network",
        )

        await manager._mark_auth_success(token.id)

        refreshed = await self.db.get_token(token.id)
        self.assertTrue(refreshed.is_active)
        self.assertEqual("ok", refreshed.auth_state)
        self.assertEqual(0, refreshed.auth_failure_count)
        self.assertIsNone(refreshed.auth_next_retry_at)
        self.assertEqual("", refreshed.last_auth_error_class)

    async def test_complete_refresh_failure_without_profile_requires_relogin_without_disabling(self):
        token = await self._add_token("refresh-failure")
        manager = self._manager()
        manager._do_refresh_at = AsyncMock(return_value=False)
        manager._try_refresh_st = AsyncMock(return_value=None)

        with patch.object(manager, "disable_token", wraps=manager.disable_token) as disable:
            result = await manager._refresh_at_inner(token.id)

        refreshed = await self.db.get_token(token.id)
        self.assertFalse(result)
        self.assertTrue(refreshed.is_active)
        self.assertEqual("reauth_required", refreshed.auth_state)
        self.assertEqual("profile_missing", refreshed.last_auth_error_class)
        disable.assert_not_awaited()

    async def test_credential_update_does_not_enable_manually_disabled_account(self):
        token = await self._add_token("manual-disable")
        await self.db.update_token(token.id, is_active=False)
        manager = self._manager()

        await manager.update_token(
            token.id,
            at="synthetic-at-updated",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        refreshed = await self.db.get_token(token.id)
        self.assertFalse(refreshed.is_active)

    async def test_refresh_calls_for_one_account_are_single_flight(self):
        token = await self._add_token("single-flight")
        manager = self._manager()
        entered = asyncio.Event()
        release = asyncio.Event()
        attempts = 0

        async def real_attempt(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            entered.set()
            await release.wait()
            return True

        manager._do_refresh_at = AsyncMock(side_effect=real_attempt)
        first = asyncio.create_task(manager._refresh_at(token.id))
        await entered.wait()
        second = asyncio.create_task(manager._refresh_at(token.id))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual([True, True], await asyncio.gather(first, second))
        self.assertEqual(1, attempts)

    async def test_refresh_chain_uses_persistent_profile_recovery_before_backoff(self):
        token = await self._add_token("profile-chain")
        await self.db.update_token(token.id, account_profile_key="a" * 32)
        manager = self._manager()
        manager._do_refresh_at = AsyncMock(return_value=False)
        manager._try_refresh_st = AsyncMock(return_value=None)
        manager._try_persistent_profile_recovery = AsyncMock(return_value=True)

        result = await manager._refresh_at_inner(token.id)

        self.assertTrue(result)
        manager._try_persistent_profile_recovery.assert_awaited_once()
        refreshed = await self.db.get_token(token.id)
        self.assertEqual("ok", refreshed.auth_state)

    async def test_persistent_profile_recovery_updates_matching_identity_and_always_closes(self):
        token = await self._add_token("profile-success")
        store = AccountProfileStore(Path(self._temp_dir.name) / "profiles")
        profile_key = store.create_key()
        store.resolve(profile_key, create=True)
        bootstrap_google_cookies = "synthetic-google-cookie-backup"
        await self.db.update_token(
            token.id,
            account_profile_key=profile_key,
            google_cookies=bootstrap_google_cookies,
        )
        token = await self.db.get_token(token.id)
        flow_client = SimpleNamespace(
            proxy_manager=None,
            get_request_fingerprint=lambda: {},
            _set_request_fingerprint=lambda _value: None,
            st_to_at=AsyncMock(
                return_value={
                    "access_token": "synthetic-at-recovered",
                    "expires": "2030-01-01T00:00:00Z",
                    "user": {"email": token.email},
                }
            ),
        )
        manager = TokenManager(self.db, flow_client, account_profile_store=store)
        captured = {}

        class FakeBrowser:
            def __init__(self, *_args, **kwargs):
                captured["kwargs"] = dict(kwargs)
                self.close = AsyncMock()
                captured["instance"] = self

            async def capture_account_onboarding_result(self, **kwargs):
                captured["capture_kwargs"] = dict(kwargs)
                return {
                    "st": "synthetic-st-recovered",
                    "google_cookies": "synthetic-cookie-recovered",
                }

        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            new=FakeBrowser,
        ):
            result = await manager._try_persistent_profile_recovery(token)

        refreshed = await self.db.get_token(token.id)
        self.assertTrue(result)
        self.assertEqual("synthetic-st-recovered", refreshed.st)
        self.assertEqual("ok", refreshed.auth_state)
        self.assertEqual(profile_key, refreshed.account_profile_key)
        self.assertNotIn("force_headed", captured["kwargs"])
        self.assertIn("persistent_profile_dir", captured["kwargs"])
        self.assertEqual(
            bootstrap_google_cookies,
            captured["capture_kwargs"]["bootstrap_google_cookies"],
        )
        captured["instance"].close.assert_awaited_once()

    async def test_persistent_profile_recovery_identity_mismatch_fails_closed(self):
        token = await self._add_token("profile-mismatch")
        store = AccountProfileStore(Path(self._temp_dir.name) / "profiles")
        profile_key = store.create_key()
        store.resolve(profile_key, create=True)
        await self.db.update_token(token.id, account_profile_key=profile_key)
        token = await self.db.get_token(token.id)
        flow_client = SimpleNamespace(
            proxy_manager=None,
            get_request_fingerprint=lambda: {},
            _set_request_fingerprint=lambda _value: None,
            st_to_at=AsyncMock(
                return_value={
                    "access_token": "synthetic-at-other",
                    "expires": "2030-01-01T00:00:00Z",
                    "user": {"email": "different@example.invalid"},
                }
            ),
        )
        manager = TokenManager(self.db, flow_client, account_profile_store=store)
        browser = SimpleNamespace(
            capture_account_onboarding_result=AsyncMock(
                return_value={
                    "st": "synthetic-st-other",
                    "google_cookies": "synthetic-cookie-other",
                }
            ),
            close=AsyncMock(),
        )

        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            return_value=browser,
        ):
            result = await manager._try_persistent_profile_recovery(token)

        refreshed = await self.db.get_token(token.id)
        self.assertFalse(result)
        self.assertEqual(token.st, refreshed.st)
        self.assertEqual("reauth_required", refreshed.auth_state)
        self.assertEqual("identity_mismatch", refreshed.last_auth_error_class)
        browser.close.assert_awaited_once()

    async def test_persistent_profile_recovery_timeout_uses_bounded_backoff_before_relogin(self):
        token = await self._add_token("profile-timeout")
        store = AccountProfileStore(Path(self._temp_dir.name) / "profiles")
        profile_key = store.create_key()
        store.resolve(profile_key, create=True)
        await self.db.update_token(token.id, account_profile_key=profile_key)
        token = await self.db.get_token(token.id)
        manager = TokenManager(
            self.db,
            SimpleNamespace(
                proxy_manager=None,
                get_request_fingerprint=lambda: {},
                _set_request_fingerprint=lambda _value: None,
            ),
            account_profile_store=store,
        )
        browser = SimpleNamespace(
            capture_account_onboarding_result=AsyncMock(side_effect=TimeoutError()),
            close=AsyncMock(),
        )

        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            return_value=browser,
        ):
            first = await manager._try_persistent_profile_recovery(token)
            after_first = await self.db.get_token(token.id)
            second = await manager._try_persistent_profile_recovery(after_first)
            after_second = await self.db.get_token(token.id)
            third = await manager._try_persistent_profile_recovery(after_second)

        refreshed = await self.db.get_token(token.id)
        self.assertFalse(first)
        self.assertFalse(second)
        self.assertFalse(third)
        self.assertEqual("backoff", after_first.auth_state)
        self.assertEqual(1, after_first.auth_failure_count)
        self.assertIsNotNone(after_first.auth_next_retry_at)
        self.assertEqual("backoff", after_second.auth_state)
        self.assertEqual(2, after_second.auth_failure_count)
        self.assertIsNotNone(after_second.auth_next_retry_at)
        self.assertEqual("reauth_required", refreshed.auth_state)
        self.assertEqual(3, refreshed.auth_failure_count)
        self.assertEqual("interactive_verification", refreshed.last_auth_error_class)
        self.assertEqual(3, browser.close.await_count)

    async def test_missing_persistent_profile_requires_relogin_without_starting_browser(self):
        token = await self._add_token("profile-missing")
        store = AccountProfileStore(Path(self._temp_dir.name) / "profiles")
        profile_key = store.create_key()
        await self.db.update_token(token.id, account_profile_key=profile_key)
        token = await self.db.get_token(token.id)
        manager = TokenManager(
            self.db,
            SimpleNamespace(
                proxy_manager=None,
                get_request_fingerprint=lambda: {},
                _set_request_fingerprint=lambda _value: None,
            ),
            account_profile_store=store,
        )

        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            side_effect=AssertionError("missing profile must not start a browser"),
        ) as browser_factory:
            result = await manager._try_persistent_profile_recovery(token)

        refreshed = await self.db.get_token(token.id)
        self.assertFalse(result)
        self.assertEqual("reauth_required", refreshed.auth_state)
        self.assertEqual("profile_missing", refreshed.last_auth_error_class)
        browser_factory.assert_not_called()

    async def test_protocol_st_candidate_is_not_persisted_before_identity_verification(self):
        token = await self._add_token("protocol-candidate")
        token.google_cookies = "synthetic-cookie"
        token.protocol_mode = "protocol"
        original_st = token.st
        manager = self._manager()

        with patch(
            "src.services.protocol_login.protocol_loginer.login",
            new=AsyncMock(
                return_value={
                    "success": True,
                    "session_token": "synthetic-candidate-st",
                }
            ),
        ):
            candidate = await manager._try_protocol_refresh_st(token.id, token)

        refreshed = await self.db.get_token(token.id)
        self.assertEqual("synthetic-candidate-st", candidate)
        self.assertEqual(original_st, refreshed.st)
        self.assertEqual("candidate_ready", refreshed.last_st_refresh_result)

    async def test_at_refresh_identity_mismatch_never_persists_candidate_credentials(self):
        token = await self._add_token("at-identity-mismatch")
        original_st = token.st
        flow_client = SimpleNamespace(
            proxy_manager=None,
            get_request_fingerprint=lambda: {},
            _set_request_fingerprint=lambda _value: None,
            st_to_at=AsyncMock(
                return_value={
                    "access_token": "synthetic-at-other",
                    "expires": "2030-01-01T00:00:00Z",
                    "user": {"email": "other@example.invalid"},
                }
            ),
            get_credits=AsyncMock(return_value={"credits": 1}),
        )
        manager = TokenManager(self.db, flow_client)

        result = await manager._do_refresh_at(
            token.id,
            "synthetic-candidate-st-other",
            token,
        )

        refreshed = await self.db.get_token(token.id)
        self.assertFalse(result)
        self.assertEqual(original_st, refreshed.st)
        self.assertIsNone(refreshed.at)
        self.assertEqual(token.email, refreshed.email)
        self.assertEqual("reauth_required", refreshed.auth_state)
        self.assertEqual("identity_mismatch", refreshed.last_auth_error_class)

    async def test_personal_browser_st_candidate_is_not_persisted_before_at_identity_check(self):
        token = await self._add_token("personal-candidate")
        await self.db.update_token(token.id, current_project_id="synthetic-project")
        token = await self.db.get_token(token.id)
        original_st = token.st
        manager = self._manager()
        manager._try_protocol_refresh_st = AsyncMock(return_value=None)
        service = SimpleNamespace(
            refresh_session_token=AsyncMock(return_value="synthetic-personal-candidate-st")
        )

        original_mode = config.captcha_method
        config.set_captcha_method("personal")
        try:
            with patch(
                "src.services.browser_captcha_personal.BrowserCaptchaService.get_instance",
                new=AsyncMock(return_value=service),
            ):
                candidate = await manager._try_refresh_st(token.id, token)
        finally:
            config.set_captcha_method(original_mode)

        refreshed = await self.db.get_token(token.id)
        self.assertEqual("synthetic-personal-candidate-st", candidate)
        self.assertEqual(original_st, refreshed.st)

    async def test_protocol_refresh_failure_keeps_healthy_at_without_interactive_profile_recovery(self):
        token = await self._add_token("protocol-healthy-at")
        store = AccountProfileStore(Path(self._temp_dir.name) / "healthy-at-profiles")
        profile_key = store.create_key()
        store.resolve(profile_key, create=True)
        await self.db.update_token(
            token.id,
            google_cookies="synthetic-cookie",
            account_profile_key=profile_key,
            at="synthetic-current-at",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=2),
            auth_state="ok",
            auth_failure_count=0,
            last_auth_error_class="",
        )
        token = await self.db.get_token(token.id)
        manager = TokenManager(
            self.db,
            SimpleNamespace(
                proxy_manager=None,
                get_request_fingerprint=lambda: {},
                _set_request_fingerprint=lambda _value: None,
            ),
            account_profile_store=store,
        )
        browser = SimpleNamespace(
            capture_account_onboarding_result=AsyncMock(side_effect=TimeoutError()),
            close=AsyncMock(),
        )

        with patch(
            "src.services.protocol_login.protocol_loginer.login",
            new=AsyncMock(return_value={"success": False}),
        ), patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService",
            return_value=browser,
        ) as browser_factory:
            await manager._refresh_protocol_token(token, datetime.now(timezone.utc))

        refreshed = await self.db.get_token(token.id)
        self.assertEqual("ok", refreshed.auth_state)
        self.assertEqual(0, refreshed.auth_failure_count)
        self.assertEqual("", refreshed.last_auth_error_class)
        self.assertEqual("synthetic-current-at", refreshed.at)
        self.assertEqual("protocol_refresh_failed", refreshed.last_st_refresh_result)
        browser_factory.assert_not_called()

    async def test_protocol_refresh_failure_still_uses_profile_recovery_when_at_needs_refresh(self):
        cases = (
            ("missing", None, datetime.now(timezone.utc) + timedelta(hours=2)),
            ("unknown-expiry", "synthetic-at", None),
            ("near-expiry", "synthetic-at", datetime.now(timezone.utc) + timedelta(minutes=59)),
        )
        for suffix, at_value, expires_at in cases:
            with self.subTest(case=suffix):
                token = await self._add_token(f"protocol-recovery-{suffix}")
                await self.db.update_token(
                    token.id,
                    google_cookies="synthetic-cookie",
                    at=at_value,
                    at_expires=expires_at,
                )
                token = await self.db.get_token(token.id)
                manager = self._manager()
                manager._try_protocol_refresh_st = AsyncMock(return_value=None)
                manager._try_persistent_profile_recovery = AsyncMock(return_value=False)

                await manager._refresh_protocol_token(token, datetime.now(timezone.utc))

                manager._try_persistent_profile_recovery.assert_awaited_once()

    async def test_background_protocol_refresh_identity_mismatch_fails_closed(self):
        token = await self._add_token("background-identity-mismatch")
        await self.db.update_token(token.id, google_cookies="synthetic-cookie")
        token = await self.db.get_token(token.id)
        original_st = token.st
        manager = self._manager()
        manager._try_protocol_refresh_st = AsyncMock(return_value="synthetic-background-candidate")
        manager._st_to_at_for_token = AsyncMock(
            return_value={
                "access_token": "synthetic-background-at-other",
                "expires": "2030-01-01T00:00:00Z",
                "user": {"email": "other@example.invalid"},
            }
        )

        await manager._refresh_protocol_token(token, datetime.now(timezone.utc))

        refreshed = await self.db.get_token(token.id)
        self.assertEqual(original_st, refreshed.st)
        self.assertIsNone(refreshed.at)
        self.assertEqual(token.email, refreshed.email)
        self.assertEqual("reauth_required", refreshed.auth_state)
        self.assertEqual("identity_mismatch", refreshed.last_auth_error_class)

    async def test_protocol_refresh_failure_persists_only_stable_error_class_and_does_not_log_raw_detail(self):
        token = await self._add_token("protocol-redaction")
        token.google_cookies = "synthetic-cookie"
        token.protocol_mode = "protocol"
        manager = self._manager()
        sentinel = "SENSITIVE_FULL_URL_AND_RESPONSE_BODY"

        with patch(
            "src.services.protocol_login.protocol_loginer.login",
            new=AsyncMock(return_value={"success": False, "error": sentinel}),
        ), patch(
            "src.services.token_manager.debug_logger.log_warning"
        ) as warning:
            result = await manager._try_protocol_refresh_st(token.id, token)

        refreshed = await self.db.get_token(token.id)
        self.assertIsNone(result)
        self.assertEqual("protocol_refresh_failed", refreshed.last_st_refresh_result)
        self.assertNotIn(sentinel, str(warning.call_args_list))

    async def test_protocol_refresh_exception_persists_only_stable_error_class(self):
        token = await self._add_token("protocol-exception")
        token.google_cookies = "synthetic-cookie"
        token.protocol_mode = "protocol"
        manager = self._manager()
        sentinel = "SENSITIVE_EXCEPTION_WITH_RESPONSE_BODY"

        with patch(
            "src.services.protocol_login.protocol_loginer.login",
            new=AsyncMock(side_effect=RuntimeError(sentinel)),
        ), patch(
            "src.services.token_manager.debug_logger.log_error"
        ) as error_log:
            result = await manager._try_protocol_refresh_st(token.id, token)

        refreshed = await self.db.get_token(token.id)
        self.assertIsNone(result)
        self.assertEqual("protocol_refresh_error", refreshed.last_st_refresh_result)
        self.assertNotIn(sentinel, str(error_log.call_args_list))

    async def test_at_refresh_and_browser_refresh_logs_do_not_include_raw_exception_text(self):
        token = await self._add_token("refresh-log-redaction")
        sentinel = "SENSITIVE_AT_URL_RESPONSE_BODY"
        flow_client = SimpleNamespace(
            proxy_manager=None,
            get_request_fingerprint=lambda: {},
            _set_request_fingerprint=lambda _value: None,
            st_to_at=AsyncMock(side_effect=RuntimeError(sentinel)),
        )
        manager = TokenManager(self.db, flow_client)

        with patch(
            "src.services.token_manager.debug_logger.log_error"
        ) as error_log:
            at_result = await manager._do_refresh_at(token.id, token.st, token)
            manager._try_protocol_refresh_st = AsyncMock(side_effect=RuntimeError(sentinel))
            st_result = await manager._try_refresh_st(token.id, token)

        self.assertFalse(at_result)
        self.assertIsNone(st_result)
        self.assertNotIn(sentinel, str(error_log.call_args_list))

    async def test_protocol_st_to_at_failure_keeps_candidate_credentials_out_of_storage(self):
        token = await self._add_token("st-to-at-redaction")
        await self.db.update_token(token.id, google_cookies="synthetic-cookie")
        token = await self.db.get_token(token.id)
        original_st = token.st
        original_at = token.at
        manager = self._manager()
        sentinel = "SENSITIVE_ST_TO_AT_RESPONSE_BODY"
        manager._try_protocol_refresh_st = AsyncMock(return_value="synthetic-new-st")
        manager._st_to_at_for_token = AsyncMock(side_effect=RuntimeError(sentinel))

        with patch(
            "src.services.token_manager.debug_logger.log_error"
        ) as error_log:
            await manager._refresh_protocol_token(token, datetime.now(timezone.utc))

        refreshed = await self.db.get_token(token.id)
        self.assertEqual(original_st, refreshed.st)
        self.assertEqual(original_at, refreshed.at)
        self.assertEqual("st_to_at_failed", refreshed.last_st_refresh_result)
        self.assertNotIn(sentinel, refreshed.last_st_refresh_result)
        self.assertNotIn(sentinel, str(error_log.call_args_list))

    async def test_successful_at_validation_repairs_historical_auth_failure_without_touching_st_history(self):
        token = await self._add_token("at-check-auth-repair")
        await self.db.update_token(
            token.id,
            at="synthetic-current-at",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=2),
            last_st_refresh_result="protocol_refresh_failed",
        )
        await self.db.update_token_auth_state(
            token.id,
            state="reauth_required",
            failure_count=3,
            next_retry_at=None,
            error_class="interactive_verification",
        )
        token = await self.db.get_token(token.id)
        flow_client = SimpleNamespace(
            proxy_manager=None,
            get_request_fingerprint=lambda: {},
            _set_request_fingerprint=lambda _value: None,
            get_credits=AsyncMock(return_value={"credits": 7, "userPaygateTier": "fixture"}),
        )
        manager = TokenManager(self.db, flow_client)

        result = await manager.ensure_valid_token(token)

        self.assertIsNotNone(result)
        refreshed = await self.db.get_token(token.id)
        self.assertEqual("ok", refreshed.auth_state)
        self.assertEqual(0, refreshed.auth_failure_count)
        self.assertIsNone(refreshed.auth_next_retry_at)
        self.assertEqual("", refreshed.last_auth_error_class)
        self.assertEqual("protocol_refresh_failed", refreshed.last_st_refresh_result)
        self.assertEqual(7, refreshed.credits)

    async def test_successful_at_validation_does_not_rewrite_clean_auth_state(self):
        token = await self._add_token("at-check-clean-auth")
        await self.db.update_token(
            token.id,
            at="synthetic-current-at",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=2),
        )
        token = await self.db.get_token(token.id)
        flow_client = SimpleNamespace(
            proxy_manager=None,
            get_request_fingerprint=lambda: {},
            _set_request_fingerprint=lambda _value: None,
            get_credits=AsyncMock(return_value={"credits": 3}),
        )
        manager = TokenManager(self.db, flow_client)
        manager._mark_auth_success = AsyncMock(wraps=manager._mark_auth_success)

        result = await manager.ensure_valid_token(token)

        self.assertIsNotNone(result)
        manager._mark_auth_success.assert_not_awaited()

    async def test_at_validation_failure_does_not_log_raw_exception_text_or_fake_auth_success(self):
        token = await self._add_token("at-check-redaction")
        await self.db.update_token(
            token.id,
            at="synthetic-current-at",
            at_expires=datetime.now(timezone.utc) + timedelta(hours=2),
            last_st_refresh_result="protocol_refresh_failed",
        )
        await self.db.update_token_auth_state(
            token.id,
            state="reauth_required",
            failure_count=2,
            next_retry_at=None,
            error_class="interactive_verification",
        )
        token = await self.db.get_token(token.id)
        sentinel = "SENSITIVE_AT_VALIDATION_URL_AND_RESPONSE_BODY"
        flow_client = SimpleNamespace(
            proxy_manager=None,
            get_request_fingerprint=lambda: {},
            _set_request_fingerprint=lambda _value: None,
            get_credits=AsyncMock(side_effect=RuntimeError(sentinel)),
        )
        manager = TokenManager(self.db, flow_client)
        manager._refresh_at = AsyncMock(return_value=False)
        manager._mark_auth_success = AsyncMock(wraps=manager._mark_auth_success)

        with patch(
            "src.services.token_manager.debug_logger.log_warning"
        ) as warning_log:
            result = await manager.ensure_valid_token(token)

        self.assertIsNone(result)
        manager._refresh_at.assert_awaited_once_with(token.id)
        manager._mark_auth_success.assert_not_awaited()
        refreshed = await self.db.get_token(token.id)
        self.assertEqual("reauth_required", refreshed.auth_state)
        self.assertEqual(2, refreshed.auth_failure_count)
        self.assertEqual("interactive_verification", refreshed.last_auth_error_class)
        self.assertEqual("protocol_refresh_failed", refreshed.last_st_refresh_result)
        self.assertNotIn(sentinel, str(warning_log.call_args_list))

    async def test_protocol_refresher_shutdown_does_not_log_raw_exception_text(self):
        sentinel = "SENSITIVE_REFRESH_SHUTDOWN_URL_AND_RESPONSE_BODY"

        class FailingTask:
            def done(self):
                return False

            def cancel(self):
                return None

            def __await__(self):
                async def fail():
                    raise RuntimeError(sentinel)

                return fail().__await__()

        manager = self._manager()
        manager._protocol_refresher_task = FailingTask()

        with patch(
            "src.services.token_manager.debug_logger.log_warning"
        ) as warning_log:
            await manager.stop_protocol_refresher()

        self.assertIsNone(manager._protocol_refresher_task)
        self.assertNotIn(sentinel, str(warning_log.call_args_list))

    async def test_protocol_refresher_uses_auth_recovery_candidates_not_generation_active_list(self):
        database = SimpleNamespace(
            get_token_refresh_config=AsyncMock(
                return_value=SimpleNamespace(enabled=True, refresh_interval_minutes=120)
            ),
            get_auth_recovery_candidates=AsyncMock(return_value=[]),
            get_active_tokens=AsyncMock(
                side_effect=AssertionError("generation active list is not the recovery queue")
            ),
        )
        manager = self._manager(database)

        await manager.run_protocol_refresh_once()

        database.get_auth_recovery_candidates.assert_awaited_once()
        database.get_active_tokens.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
