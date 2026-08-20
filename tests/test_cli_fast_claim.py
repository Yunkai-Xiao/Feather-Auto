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
                    "stagecraftClaimStage": {
                        "id": "task-1",
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

    def test_claim_payload_uses_current_stagecraft_operation(self) -> None:
        self.assertEqual(
            cli.claim_payload("task-1"),
            [
                {
                    "operationName": "StagecraftClaimStage",
                    "variables": {"taskId": "task-1", "stageKey": "task"},
                    "query": cli.STAGECRAFT_CLAIM_STAGE_QUERY,
                }
            ],
        )

    def test_stagecraft_search_response_is_normalized_for_existing_filters(self) -> None:
        body = [
            {
                "data": {
                    "stagecraftSearch": {
                        "results": [
                            {
                                "task": {
                                    "id": "task-1",
                                    "title": "Slide task",
                                    "statusUpdatedAt": "2026-08-19T03:01:38Z",
                                    "tags": ["one", "two"],
                                    "stagecraftTaskStatus": "CLAIMABLE",
                                    "isTemplateTask": False,
                                    "taskBatchName": "AESTHETIC RANKING: sample",
                                },
                                "stage": {"key": "task", "label": "Task"},
                                "previousStageLabels": [],
                                "claimedAt": None,
                                "completedAt": None,
                            }
                        ],
                        "nextCursor": "cursor-2",
                    }
                }
            }
        ]
        payload = {
            "_search_api": "stagecraft",
            "campaign_id": "campaign",
            "task_batch_id": None,
            "page_size": 20,
            "cursor": None,
        }

        normalized = cli.normalize_stagecraft_search_response(body, payload)

        self.assertEqual(normalized["tasks"][0]["id"], "task-1")
        self.assertEqual(normalized["tasks"][0]["task_batch_name"], "AESTHETIC RANKING: sample")
        self.assertEqual(normalized["tasks"][0]["kind"], "widget-layout")
        self.assertEqual(normalized["pagination"]["next_cursor"], "cursor-2")
        self.assertNotIn("count", normalized["pagination"])

    def test_stagecraft_search_restores_batch_metadata_from_task_detail(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "tasks": [
                {
                    "id": "task-1",
                    "task_batch_id": "batch-1",
                    "task_batch_name": "STYLE MATCHING: sample",
                    "kind": "widget-layout",
                }
            ]
        }
        data = {
            "tasks": [
                {
                    "id": "task-1",
                    "task_batch_name": None,
                    "tags": ["one"],
                    "kind": "widget-layout",
                }
            ],
            "pagination": {"search_api": "stagecraft"},
        }

        with patch.object(cli, "request_with_retries", return_value=response) as request:
            enriched = cli.enrich_stagecraft_search_tasks(
                data,
                {"header": "value"},
                "campaign",
                session=Mock(),
            )

        self.assertEqual(enriched["tasks"][0]["task_batch_id"], "batch-1")
        self.assertEqual(enriched["tasks"][0]["task_batch_name"], "STYLE MATCHING: sample")
        sent_payload = request.call_args.kwargs["data"]
        self.assertIn('"query":"task-1"', sent_payload)
        self.assertIn('"workflow_statuses":["unclaimed","in_progress"', sent_payload)

    def test_stagecraft_search_drops_raced_task_when_batch_metadata_is_unavailable(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"tasks": []}
        data = {
            "tasks": [{"id": "task-1", "task_batch_name": None}],
            "pagination": {"search_api": "stagecraft"},
        }

        with patch.object(cli, "request_with_retries", return_value=response):
            enriched = cli.enrich_stagecraft_search_tasks(data, {}, "campaign")

        self.assertEqual(enriched["tasks"], [])
        self.assertEqual(enriched["metadata_lookup_missing_task_ids"], ["task-1"])

    def test_headers_can_follow_metadata_from_fresh_browser_curl(self) -> None:
        headers = cli.build_headers(
            "cookie",
            "campaign",
            cli.task_stage_url("task-1"),
            task_id="task-1",
            task_kind="widget-layout",
            client_git_hash="fresh-hash",
            user_agent="fresh-agent",
        )

        self.assertEqual(headers["referer"], "https://feather.openai.com/tasks/task-1/stage/task")
        self.assertEqual(headers["x-feather-client-git-hash"], "fresh-hash")
        self.assertEqual(headers["user-agent"], "fresh-agent")

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
            patch.object(cli, "fetch_task_histories", return_value={"task-1": []}) as fetch_task_histories,
            patch.object(cli, "fetch_task_history") as fetch_task_history,
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
        fetch_task_histories.assert_called_once_with(ANY, ["task-1"], session=session)
        fetch_task_history.assert_not_called()
        self.assertIs(print_claim_result.call_args.kwargs["session"], session)
        session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
