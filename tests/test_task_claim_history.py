from __future__ import annotations

import json
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

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class TaskHistoryClaimGuardTests(unittest.TestCase):
    def test_history_payload_matches_task_history_operation(self) -> None:
        payload = cli.task_history_payload("task-1")

        self.assertEqual(payload[0]["operationName"], "TaskHistory")
        self.assertEqual(payload[0]["variables"], {"taskId": "task-1"})
        self.assertIn("taskHistory(taskId: $taskId)", payload[0]["query"])
        self.assertIn("workflowStatus", payload[0]["query"])
        self.assertIn("user {", payload[0]["query"])

    def test_fetch_history_reuses_session_and_unwraps_batched_graphql(self) -> None:
        history = [{"version": 1, "entries": []}]
        response = FakeResponse([{"data": {"taskHistory": history}}])
        session = Mock()
        session.request.return_value = response

        result = cli.fetch_task_history({"header": "value"}, "task-1", session=session)

        self.assertEqual(result, history)
        session.request.assert_called_once_with(
            "POST",
            "https://feather.openai.com/api/graphql",
            timeout=10,
            headers={"header": "value"},
            data=ANY,
        )
        request_body = json.loads(session.request.call_args.kwargs["data"])
        self.assertEqual(request_body[0]["variables"]["taskId"], "task-1")

    def test_fetch_histories_batches_multiple_tasks_in_one_request(self) -> None:
        histories = {
            "task-1": [{"version": 1, "entries": []}],
            "task-2": [{"version": 2, "entries": [{"workflowStatus": "IN_PROGRESS"}]}],
        }
        response = FakeResponse(
            [
                {"data": {"taskHistory": histories["task-1"]}},
                {"data": {"taskHistory": histories["task-2"]}},
            ]
        )
        session = Mock()
        session.request.return_value = response

        result = cli.fetch_task_histories({}, ["task-1", "task-2"], session=session)

        self.assertEqual(result, histories)
        session.request.assert_called_once()
        request_body = json.loads(session.request.call_args.kwargs["data"])
        self.assertEqual(
            [item["variables"]["taskId"] for item in request_body],
            ["task-1", "task-2"],
        )

    def test_previously_claimed_includes_in_progress_and_completed(self) -> None:
        self.assertTrue(
            cli.was_previously_claimed(
                [{"version": 1, "entries": [{"workflowStatus": "IN_PROGRESS"}]}]
            )
        )
        self.assertTrue(
            cli.was_previously_claimed(
                [{"version": 1, "entries": [{"workflowStatus": "COMPLETED"}]}]
            )
        )
        self.assertFalse(
            cli.was_previously_claimed(
                [{"version": 1, "entries": [{"workflowStatus": "UNCLAIMED"}]}]
            )
        )

    def test_other_users_completed_entry_blocks_claim(self) -> None:
        history = [
            {
                "version": 2,
                "entries": [
                    {
                        "__typename": "TaskStatusHistoryEntry",
                        "workflowStatus": "COMPLETED",
                        "createdAt": "2026-07-29T12:00:00Z",
                        "user": {"id": "user-2", "email": "other@example.com"},
                    }
                ],
            }
        ]

        evidence = cli.completed_history_evidence(history)

        self.assertEqual(
            evidence,
            {
                "history_version": 2,
                "completed_at": "2026-07-29T12:00:00Z",
                "completed_by_user_id": "user-2",
                "completed_by_user_email": "other@example.com",
                "completed_by_unknown_user": False,
            },
        )

    def test_current_feather_users_completed_entry_still_blocks_shared_account(self) -> None:
        history = [
            {
                "version": 1,
                "entries": [
                    {
                        "workflowStatus": "completed",
                        "user": {"id": "user-1", "email": "old-address@example.com"},
                    }
                ],
            }
        ]

        evidence = cli.completed_history_evidence(history)

        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["completed_by_user_id"], "user-1")

    def test_completed_entry_without_actor_fails_closed(self) -> None:
        history = [{"version": 3, "entries": [{"workflowStatus": "COMPLETED", "user": None}]}]

        evidence = cli.completed_history_evidence(history)

        self.assertIsNotNone(evidence)
        self.assertTrue(evidence["completed_by_unknown_user"])

    def test_monitor_skips_completed_task_and_claims_next_candidate(self) -> None:
        completed_task = {"id": "task-old", "title": "Old task", "tags": []}
        fresh_task = {"id": "task-fresh", "title": "Fresh task", "tags": []}
        response = {
            "tasks": [completed_task, fresh_task],
            "pagination": {"page": 0, "page_size": 20, "count": 2},
        }
        completed_history = [
            {
                "version": 1,
                "entries": [
                    {
                        "workflowStatus": "COMPLETED",
                        "user": {"id": "user-2", "email": "other@example.com"},
                    }
                ],
            }
        ]
        session = Mock()
        events: list[str] = []
        statuses: list[dict[str, object]] = []
        config = cli.MonitorConfig(campaign_id="campaign", once=True, claim=True, save=None)

        with (
            patch.object(cli, "create_http_session", return_value=session),
            patch.object(cli, "read_curl_text", return_value="curl"),
            patch.object(
                cli,
                "request_parts_from_curl",
                return_value=("cookie", {"page": 0, "page_size": 20}),
            ),
            patch.object(cli, "current_user", return_value={"id": "user-1", "email": "me@example.com"}),
            patch.object(cli, "find_current_in_progress_task", return_value=None),
            patch.object(
                cli,
                "resolve_batch_searches",
                return_value=([], [(None, {"page": 0, "page_size": 20})]),
            ),
            patch.object(cli, "poll_all_pages", return_value=[response]),
            patch.object(
                cli,
                "fetch_task_histories",
                return_value={"task-old": completed_history, "task-fresh": []},
            ) as history_check,
            patch.object(cli, "print_claim_result", return_value=True) as claim,
        ):
            result = cli.run_monitor(
                config,
                emit=lambda *values, **_kwargs: events.append(
                    " ".join(str(value) for value in values)
                ),
                status_callback=statuses.append,
            )

        self.assertEqual(result, 0)
        history_check.assert_called_once_with(
            ANY,
            ["task-old", "task-fresh"],
            session=session,
        )
        claim.assert_called_once()
        self.assertEqual(claim.call_args.args[4], "task-fresh")
        self.assertTrue(any(event.startswith("CLAIM_SKIPPED_PREVIOUSLY_COMPLETED task=task-old") for event in events))
        poll_status = next(status for status in statuses if status.get("history_sample_complete"))
        self.assertEqual(poll_status["previously_claimed_count"], 1)
        self.assertEqual(poll_status["claim_history_checked_count"], 2)

    def test_history_timeout_skips_claim_and_retries_task_on_next_poll(self) -> None:
        task = {"id": "task-1", "title": "Task", "tags": []}
        response = {
            "tasks": [task],
            "pagination": {"page": 0, "page_size": 20, "count": 1},
        }
        session = Mock()
        config = cli.MonitorConfig(campaign_id="campaign", once=True, claim=True, save=None)

        with (
            patch.object(cli, "create_http_session", return_value=session),
            patch.object(cli, "read_curl_text", return_value="curl"),
            patch.object(
                cli,
                "request_parts_from_curl",
                return_value=("cookie", {"page": 0, "page_size": 20}),
            ),
            patch.object(cli, "current_user", return_value={"id": "user-1"}),
            patch.object(cli, "find_current_in_progress_task", return_value=None),
            patch.object(
                cli,
                "resolve_batch_searches",
                return_value=([], [(None, {"page": 0, "page_size": 20})]),
            ),
            patch.object(cli, "poll_all_pages", return_value=[response]),
            patch.object(
                cli,
                "fetch_task_histories",
                side_effect=requests.exceptions.Timeout("history timed out"),
            ),
            patch.object(cli, "print_claim_result") as claim,
        ):
            result = cli.run_monitor(config, emit=lambda *_args, **_kwargs: None)

        self.assertEqual(result, 0)
        claim.assert_not_called()

    def test_ineligible_task_does_not_request_history(self) -> None:
        task = {"id": "task-1", "title": "Task", "tags": []}
        response = {
            "tasks": [task],
            "pagination": {"page": 0, "page_size": 20, "count": 1},
        }
        session = Mock()
        config = cli.MonitorConfig(
            campaign_id="campaign",
            once=True,
            claim=True,
            tag_count_min=1,
            save=None,
        )

        with (
            patch.object(cli, "create_http_session", return_value=session),
            patch.object(cli, "read_curl_text", return_value="curl"),
            patch.object(
                cli,
                "request_parts_from_curl",
                return_value=("cookie", {"page": 0, "page_size": 20}),
            ),
            patch.object(cli, "current_user", return_value={"id": "user-1"}),
            patch.object(cli, "find_current_in_progress_task", return_value=None),
            patch.object(
                cli,
                "resolve_batch_searches",
                return_value=([], [(None, {"page": 0, "page_size": 20})]),
            ),
            patch.object(cli, "poll_all_pages", return_value=[response]),
            patch.object(cli, "fetch_task_histories") as history_check,
            patch.object(cli, "print_claim_result") as claim,
        ):
            result = cli.run_monitor(config, emit=lambda *_args, **_kwargs: None)

        self.assertEqual(result, 0)
        history_check.assert_not_called()
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
