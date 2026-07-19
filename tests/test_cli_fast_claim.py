from __future__ import annotations

import unittest
from unittest.mock import ANY, Mock, patch

import requests

from feather_auto import cli


class FakeResponse:
    def __init__(self, body: object, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.text = "response text"

    def json(self) -> object:
        return self._body


class ConnectionReuseAndFastClaimTests(unittest.TestCase):
    def test_credential_summary_exposes_identity_without_secrets(self) -> None:
        summary = cli.credential_account_summary(
            {
                "id": "user-1",
                "email": "person@example.com",
                "name": "Person Example",
                "cookie": "secret-cookie",
                "access_token": "secret-token",
            }
        )

        self.assertEqual(
            summary,
            {
                "label": "person@example.com",
                "email": "person@example.com",
                "display_name": "Person Example",
                "user_id": "user-1",
            },
        )
        self.assertNotIn("cookie", summary)
        self.assertNotIn("access_token", summary)

    def test_observe_mode_reports_verified_credential_account(self) -> None:
        session = Mock()
        statuses: list[dict[str, object]] = []
        account = {"id": "user-1", "email": "person@example.com"}
        config = cli.MonitorConfig(campaign_id="campaign", once=True, save=None)

        with (
            patch.object(cli, "create_http_session", return_value=session),
            patch.object(cli, "read_curl_text", return_value="curl"),
            patch.object(cli, "request_parts_from_curl", return_value=("cookie", {"page": 0, "page_size": 20})),
            patch.object(cli, "current_user", return_value=account) as current_user,
            patch.object(cli, "resolve_batch_searches", return_value=([], [(None, {"page": 0, "page_size": 20})])),
            patch.object(cli, "poll_all_pages", return_value=[]),
        ):
            result = cli.run_monitor(
                config,
                emit=lambda *_args, **_kwargs: None,
                status_callback=statuses.append,
            )

        self.assertEqual(result, 0)
        current_user.assert_called_once_with(ANY, session=session)
        self.assertTrue(statuses)
        self.assertEqual(statuses[-1]["credential_account"], cli.credential_account_summary(account))
        session.close.assert_called_once_with()

    def test_request_helper_uses_supplied_session(self) -> None:
        response = FakeResponse({})
        session = Mock()
        session.request.return_value = response

        with patch.object(requests, "request") as global_request:
            result = cli.request_with_retries("GET", "https://example.test", session=session)

        self.assertIs(result, response)
        session.request.assert_called_once_with("GET", "https://example.test", timeout=10)
        global_request.assert_not_called()

    def test_definitive_graphql_success_skips_follow_up_verification(self) -> None:
        response = FakeResponse(
            {
                "data": {
                    "updateTaskStatus": {
                        "id": "task-1",
                        "workflowStatus": "in_progress",
                    }
                }
            }
        )
        session = Mock()

        with (
            patch.object(cli, "claim_task", return_value=response) as claim_task,
            patch.object(cli, "current_user") as current_user,
            patch.object(cli, "verify_task_assignment") as verify_task_assignment,
        ):
            succeeded = cli.print_claim_result(
                {},
                {},
                "campaign",
                20,
                "task-1",
                emit=lambda *_args, **_kwargs: None,
                session=session,
                user={"id": "user-1"},
            )

        self.assertTrue(succeeded)
        claim_task.assert_called_once_with({}, "task-1", session=session)
        current_user.assert_not_called()
        verify_task_assignment.assert_not_called()

    def test_claim_runs_before_found_log_status_and_artifact_write(self) -> None:
        task = {"id": "task-1", "title": "Fast task", "tags": []}
        response = {"tasks": [task], "pagination": {"page": 0, "page_size": 20, "count": 1}}
        session = Mock()
        events: list[str] = []

        def emit(*values: object, **_kwargs: object) -> None:
            events.append("emit:" + " ".join(str(value) for value in values))

        def claim(*_args: object, **_kwargs: object) -> bool:
            events.append("claim")
            return True

        def save(*_args: object, **_kwargs: object) -> None:
            events.append("save")

        config = cli.MonitorConfig(
            campaign_id="campaign",
            once=True,
            claim=True,
            save="found.json",
        )

        with (
            patch.object(cli, "create_http_session", return_value=session),
            patch.object(cli, "read_curl_text", return_value="curl"),
            patch.object(cli, "request_parts_from_curl", return_value=("cookie", {"page": 0, "page_size": 20})),
            patch.object(cli, "current_user", return_value={"id": "user-1"}) as current_user,
            patch.object(cli, "find_current_in_progress_task", return_value=None) as in_progress_guard,
            patch.object(cli, "resolve_batch_searches", return_value=([], [(None, {"page": 0, "page_size": 20})])) as resolve_searches,
            patch.object(cli, "poll_all_pages", return_value=[response]) as poll_all_pages,
            patch.object(cli, "print_claim_result", side_effect=claim) as print_claim_result,
            patch.object(cli, "save_found", side_effect=save),
        ):
            result = cli.run_monitor(config, emit=emit)

        self.assertEqual(result, 0)
        found_index = next(index for index, event in enumerate(events) if event.startswith("emit:FOUND "))
        self.assertLess(events.index("claim"), found_index)
        self.assertLess(events.index("claim"), events.index("save"))
        current_user.assert_called_once_with(ANY, session=session)
        self.assertEqual(in_progress_guard.call_count, 2)
        self.assertTrue(all(call.kwargs.get("session") is session for call in in_progress_guard.call_args_list))
        self.assertIs(resolve_searches.call_args.kwargs["session"], session)
        self.assertIs(poll_all_pages.call_args.kwargs["session"], session)
        self.assertIs(print_claim_result.call_args.kwargs["session"], session)
        session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
