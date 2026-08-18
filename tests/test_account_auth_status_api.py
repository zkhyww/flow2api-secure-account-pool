import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from src.api import admin


class AccountAuthStatusProjectionTests(unittest.TestCase):
    def test_only_stable_public_auth_states_are_emitted(self):
        now = datetime.now(timezone.utc)
        cases = [
            (
                {"is_active": False, "auth_state": "ok", "has_account_profile": True},
                ("已停用", 0, False),
            ),
            (
                {"is_active": True, "auth_state": "ok", "has_account_profile": True},
                ("正常", 0, False),
            ),
            (
                {"is_active": True, "auth_state": "refresh_pending", "has_account_profile": True},
                ("等待自动恢复", 0, False),
            ),
            (
                {
                    "is_active": True,
                    "auth_state": "backoff",
                    "has_account_profile": True,
                    "auth_next_retry_at": now + timedelta(seconds=45),
                },
                ("稍后重试", 45, False),
            ),
            (
                {"is_active": True, "auth_state": "reauth_required", "has_account_profile": True},
                ("需要重新登录", 0, True),
            ),
            (
                {"is_active": True, "auth_state": "ok", "has_account_profile": False},
                ("需要重新登录", 0, True),
            ),
        ]

        allowed = {"正常", "等待自动恢复", "稍后重试", "需要重新登录", "已停用"}
        for row, expected in cases:
            with self.subTest(expected=expected[0]):
                status, retry_after, can_reauth = admin._project_public_auth_status(row, now=now)
                self.assertEqual(expected[0], status)
                self.assertIn(status, allowed)
                self.assertLessEqual(abs(expected[1] - retry_after), 1)
                self.assertEqual(expected[2], can_reauth)

    def test_unknown_internal_auth_state_fails_closed_to_relogin(self):
        status, retry_after, can_reauth = admin._project_public_auth_status(
            {
                "is_active": True,
                "auth_state": "unexpected_internal_value",
                "has_account_profile": True,
            },
            now=datetime.now(timezone.utc),
        )

        self.assertEqual("需要重新登录", status)
        self.assertEqual(0, retry_after)
        self.assertTrue(can_reauth)


class AccountAuthRefreshApiPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_token_manager = admin.token_manager

    async def asyncTearDown(self):
        admin.token_manager = self._original_token_manager

    async def test_manual_at_refresh_does_not_return_or_log_raw_exception_text(self):
        sentinel = "SENSITIVE_MANUAL_REFRESH_URL_AND_RESPONSE_BODY"
        admin.token_manager = SimpleNamespace(
            _refresh_at=AsyncMock(side_effect=RuntimeError(sentinel))
        )

        with patch("src.core.logger.debug_logger.log_error") as error_log:
            with self.assertRaises(HTTPException) as raised:
                await admin.refresh_at(7, token="synthetic-admin")

        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual("刷新AT失败", raised.exception.detail)
        self.assertNotIn(sentinel, str(error_log.call_args_list))


if __name__ == "__main__":
    unittest.main()
