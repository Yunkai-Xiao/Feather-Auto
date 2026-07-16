from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

from feather_auto import cli


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    def __init__(self, body: object, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.text = "response text"

    def json(self) -> object:
        return self._body


class LowLatencyMonitorTests(unittest.TestCase):
    def base_config(self, **changes: object) -> cli.MonitorConfig:
        values: dict[str, object] = {
            "campaign_id": "campaign",
            "once": True,
            "batch_regex": "Aesthetic",
            "save": None,
            "interval": 0.1,
            "poll_workers": 4,
        }
        values.update(changes)
        return cli.MonitorConfig(**values)

    def network_patches(
        self,
        poll_once: Mock,
        *,
        claim_result: Mock | None = None,
        guard: Mock | None = None,
    ) -> tuple[object, ...]:
        refs = [
            {"id": "batch-fast", "name": "Aesthetic Fast", "status": "active", "is_archived": False},
            {"id": "batch-slow", "name": "Aesthetic Slow", "status": "active", "is_archived": False},
        ]
        searches = [
            ("batch-fast", {"task_batch_id": "batch-fast"}),
            ("batch-slow", {"task_batch_id": "batch-slow"}),
        ]
        return (
            patch.object(cli, "create_http_session", side_effect=lambda _size=1: FakeSession()),
            patch.object(cli, "read_curl_text", return_value="curl"),
            patch.object(cli, "request_parts_from_curl", return_value=("cookie", {})),
            patch.object(cli, "current_user", return_value={"id": "user-1"}),
            patch.object(
                cli,
                "find_current_in_progress_task",
                guard or Mock(return_value=None),
            ),
            patch.object(cli, "resolve_batch_searches", return_value=(refs, searches)),
            patch.object(cli, "poll_once", poll_once),
            patch.object(
                cli,
                "print_claim_result",
                claim_result or Mock(return_value=True),
            ),
        )

    def test_batch_searches_run_concurrently(self) -> None:
        barrier = threading.Barrier(3, timeout=1.0)
        thread_ids: set[int] = set()

        def poll(_headers: object, _payload: object, session: object = None) -> dict[str, object]:
            del session
            thread_ids.add(threading.get_ident())
            barrier.wait()
            return {"tasks": []}

        poll_mock = Mock(side_effect=poll)
        patches = self.network_patches(poll_mock)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            result = cli.run_monitor(self.base_config(), emit=lambda *_args, **_kwargs: None)

        self.assertEqual(result, 0)
        self.assertEqual(len(thread_ids), 3)
        self.assertEqual(poll_mock.call_count, 3)

    def test_eligible_task_is_claimed_before_slow_batch_finishes_or_found_io(self) -> None:
        slow_started = threading.Event()
        release_slow = threading.Event()
        slow_finished = threading.Event()
        claim_called = threading.Event()
        emitted: list[str] = []
        statuses: list[dict[str, object]] = []

        task = {
            "id": "task-1",
            "title": "Fast task",
            "kind": "widget-layout",
            "task_batch_id": "batch-fast",
            "task_batch_name": "Aesthetic Fast",
            "tags": [],
        }

        def poll(_headers: object, payload: dict[str, object], session: object = None) -> dict[str, object]:
            del session
            batch_id = payload.get("task_batch_id")
            if batch_id == "batch-slow":
                slow_started.set()
                release_slow.wait(2.0)
                slow_finished.set()
                return {"tasks": []}
            if batch_id == "batch-fast":
                self.assertTrue(slow_started.wait(1.0))
                return {"tasks": [task]}
            return {"tasks": []}

        def claim(*_args: object, **_kwargs: object) -> bool:
            self.assertFalse(slow_finished.is_set())
            self.assertFalse(any(line.startswith("FOUND ") for line in emitted))
            self.assertFalse(any(status.get("state") == "found" for status in statuses))
            claim_called.set()
            return True

        guard = Mock(return_value=None)
        patches = self.network_patches(
            Mock(side_effect=poll),
            claim_result=Mock(side_effect=claim),
            guard=guard,
        )

        def emit(*values: object, **_kwargs: object) -> None:
            emitted.append(" ".join(str(value) for value in values))

        try:
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
                result = cli.run_monitor(
                    self.base_config(claim=True),
                    emit=emit,
                    status_callback=statuses.append,
                )
            self.assertEqual(result, 0)
            self.assertTrue(claim_called.is_set())
            self.assertFalse(slow_finished.is_set())
            self.assertEqual(guard.call_count, 1)
            self.assertEqual(statuses[-1]["state"], "claimed")
            self.assertIn("claim_dispatch_ms", statuses[-1])
        finally:
            release_slow.set()
            slow_finished.wait(2.0)

    def test_definitive_graphql_success_skips_follow_up_search(self) -> None:
        task_id = "task-1"
        response = FakeResponse(
            {
                "data": {
                    "updateTaskStatus": {
                        "id": task_id,
                        "workflowStatus": "in_progress",
                    }
                }
            }
        )
        with (
            patch.object(cli, "claim_task", return_value=response),
            patch.object(cli, "current_user") as current_user,
            patch.object(cli, "verify_task_assignment") as verify,
        ):
            succeeded = cli.print_claim_result(
                {},
                {},
                "campaign",
                20,
                task_id,
                emit=lambda *_args, **_kwargs: None,
            )

        self.assertTrue(succeeded)
        current_user.assert_not_called()
        verify.assert_not_called()

    def test_definitive_graphql_error_skips_pointless_verification(self) -> None:
        response = FakeResponse([{"data": None, "errors": [{"message": "Task not found"}]}])
        with (
            patch.object(cli, "claim_task", return_value=response),
            patch.object(cli, "current_user") as current_user,
            patch.object(cli, "verify_task_assignment") as verify,
        ):
            succeeded = cli.print_claim_result(
                {},
                {},
                "campaign",
                20,
                "task-1",
                emit=lambda *_args, **_kwargs: None,
            )

        self.assertFalse(succeeded)
        current_user.assert_not_called()
        verify.assert_not_called()

    def test_request_helper_uses_supplied_persistent_session(self) -> None:
        session = Mock()
        session.request.return_value = FakeResponse({})
        with patch.object(requests, "request") as global_request:
            response = cli.request_with_retries("GET", "https://example.test", session=session)

        self.assertIs(response, session.request.return_value)
        session.request.assert_called_once()
        global_request.assert_not_called()

    def test_fast_lane_can_match_a_new_batch_before_refs_refresh(self) -> None:
        stale_refs = [{"id": "old", "name": "Aesthetic Old"}]
        new_task = {
            "id": "task-new",
            "task_batch_id": "new",
            "task_batch_name": "Aesthetic Just Added",
            "tags": [],
        }
        unrelated_task = {
            "id": "task-other",
            "task_batch_id": "other",
            "task_batch_name": "Unrelated",
            "title": "An Aesthetic task title in the wrong batch",
            "tags": [],
        }

        self.assertTrue(
            cli.task_matches_filters(new_task, None, None, "Aesthetic", stale_refs, None, None)
        )
        self.assertFalse(
            cli.task_matches_filters(unrelated_task, None, None, "Aesthetic", stale_refs, None, None)
        )

    def test_subsecond_interval_floor(self) -> None:
        self.assertEqual(cli.validate_interval_values(0.1, 0.1, 0.2), (0.1, 0.2))
        with self.assertRaises(SystemExit):
            cli.validate_interval_values(0.09, None, None)


if __name__ == "__main__":
    unittest.main()