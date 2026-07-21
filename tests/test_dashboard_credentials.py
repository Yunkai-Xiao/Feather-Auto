from __future__ import annotations

import unittest
from unittest.mock import ANY, Mock, patch

from feather_auto import dashboard_server


class DashboardCredentialTests(unittest.TestCase):
    def test_verify_credential_caches_only_the_account_summary(self) -> None:
        session = Mock()
        user = {
            "id": "user-1",
            "email": "person@example.com",
            "name": "Person Example",
            "cookie": "secret-cookie",
            "access_token": "secret-token",
        }

        with (
            patch.object(dashboard_server, "create_http_session", return_value=session),
            patch.object(
                dashboard_server,
                "credential_request_context",
                return_value=("campaign-1", {"cookie": "secret-cookie"}),
            ),
            patch.object(dashboard_server, "current_user", return_value=user) as current_user,
            patch.object(dashboard_server.MONITOR, "remember_credential_account") as remember,
        ):
            result = dashboard_server.verify_credential({})

        expected = {
            "label": "person@example.com",
            "email": "person@example.com",
            "display_name": "Person Example",
            "user_id": "user-1",
        }
        self.assertEqual(result["credential_account"], expected)
        self.assertNotIn("cookie", result["credential_account"])
        self.assertNotIn("access_token", result["credential_account"])
        current_user.assert_called_once_with(ANY, session=session)
        remember.assert_called_once_with(expected)
        session.close.assert_called_once_with()

    def test_regex_test_verifies_credential_with_the_same_session(self) -> None:
        session = Mock()
        refs = [
            {"id": "batch-1", "name": "Aesthetic Alpha", "status": "active"},
            {"id": "batch-2", "name": "Content Beta", "status": "active"},
        ]
        account = {
            "label": "person@example.com",
            "email": "person@example.com",
            "display_name": None,
            "user_id": "user-1",
        }

        with (
            patch.object(dashboard_server, "create_http_session", return_value=session),
            patch.object(
                dashboard_server,
                "credential_request_context",
                return_value=(dashboard_server.DEFAULT_CAMPAIGN_ID, {"cookie": "secret-cookie"}),
            ),
            patch.object(
                dashboard_server,
                "verified_credential_account",
                return_value=account,
            ) as verify,
            patch.object(dashboard_server, "active_batch_refs", return_value=refs) as active_refs,
        ):
            result = dashboard_server.test_batch_regex({"batchRegex": "Aesthetic"})

        self.assertEqual(result["credential_account"], account)
        self.assertEqual(result["match_count"], 1)
        verify.assert_called_once_with(ANY, session=session)
        active_refs.assert_called_once_with(ANY, dashboard_server.DEFAULT_CAMPAIGN_ID, session=session)
        session.close.assert_called_once_with()

    def test_unidentifiable_whoami_response_is_not_treated_as_verified(self) -> None:
        with (
            patch.object(dashboard_server, "current_user", return_value={}),
            patch.object(dashboard_server.MONITOR, "remember_credential_account") as remember,
        ):
            with self.assertRaisesRegex(RuntimeError, "did not return an identifiable"):
                dashboard_server.verified_credential_account({})

        remember.assert_not_called()


if __name__ == "__main__":
    unittest.main()
