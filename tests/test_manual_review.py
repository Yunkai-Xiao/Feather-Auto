import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from feather_auto import dashboard_server, review_task_slides


class ManualReviewTests(unittest.TestCase):
    def test_review_output_can_hide_files_from_a_previous_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_dir = root / "task-1"
            deck_dir = task_dir / "deck_reviews"
            deck_dir.mkdir(parents=True)
            combined = task_dir / "content_grading_comments.md"
            deck = deck_dir / "deck_01_content_grading_comments.md"
            combined.write_text("old combined", encoding="utf-8")
            deck.write_text("old deck", encoding="utf-8")
            os.utime(combined, (100, 100))
            os.utime(deck, (100, 100))

            with patch.object(dashboard_server, "REVIEW_OUTPUT_ROOT", root):
                hidden = dashboard_server.review_output_payload("task-1", since=200)
                visible = dashboard_server.review_output_payload("task-1", since=50)

            self.assertEqual(hidden["combined_markdown"], "")
            self.assertEqual(hidden["deck_files"], [])
            self.assertEqual(visible["combined_markdown"], "old combined")
            self.assertEqual(len(visible["deck_files"]), 1)

    def test_codex_executable_honors_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "codex.exe"
            executable.write_bytes(b"test")
            with patch.dict("os.environ", {"FEATHER_REVIEW_CODEX_EXECUTABLE": str(executable)}):
                self.assertEqual(review_task_slides.codex_executable(), str(executable))

    def test_dedicated_review_page_uses_review_api(self) -> None:
        page = (Path(__file__).resolve().parent.parent / "review.html").read_text(encoding="utf-8")

        self.assertIn('id="taskId"', page)
        self.assertIn('href="/dashboard.html"', page)
        self.assertIn('/api/review-state', page)
        self.assertIn('/api/start-review', page)
        self.assertIn('/api/review-output', page)

    def test_markdown_has_one_dash_prefixed_rationale_per_line(self) -> None:
        payload = {
            "summary": {
                "deck_quality_ranking": [
                    {"rank": 1, "deck": "deck_01", "score": 7},
                ]
            },
            "decks": {
                "deck_01": {
                    "comments": [
                        {"slide": 1, "comment_draft": "First issue."},
                        {"slide": 2, "comment_draft": "- Second issue."},
                    ]
                }
            }
        }

        markdown = review_task_slides.render_content_grading_markdown(payload)

        self.assertIn("## A — Score 7\n\nBrief feedback:\n- First issue.\n- Second issue.\n", markdown)
        self.assertNotIn("Quote/phrase:", markdown)
        self.assertNotIn("Comment draft:", markdown)

    def test_relative_scores_use_unique_extremes(self) -> None:
        self.assertEqual(review_task_slides.relative_score_sequence(2), [7, 1])
        self.assertEqual(review_task_slides.relative_score_sequence(4), [7, 5, 3, 1])
        self.assertEqual(review_task_slides.relative_score_sequence(7), [7, 6, 5, 4, 3, 2, 1])
        self.assertEqual(review_task_slides.relative_score_sequence(8), [7, 6, 5, 4, 4, 3, 2, 1])

    def test_downloader_falls_back_to_task_api_without_graphql_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            auth_curl = root / "auth.curl.txt"
            auth_curl.write_text("curl 'https://feather.openai.com/api/v2/tasks/search'", encoding="utf-8")
            output_dir = root / "review"
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"downloaded": 2}',
                stderr="",
            )
            with patch.object(review_task_slides.subprocess, "run", return_value=completed) as run:
                result = review_task_slides.run_downloader(
                    "task-1",
                    auth_curl,
                    root / "missing-redirect.curl.txt",
                    root / "missing-conversation.curl.txt",
                    output_dir,
                )

            command = run.call_args.args[0]
            self.assertIn("--api", command)
            self.assertNotIn("--api-original", command)
            self.assertEqual(result["download_mode"], "api")

    def test_downloader_prefers_original_graphql_templates_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            auth_curl = root / "auth.curl.txt"
            redirect_curl = root / "redirect.curl.txt"
            conversation_curl = root / "conversation.curl.txt"
            for path in (auth_curl, redirect_curl, conversation_curl):
                path.write_text("curl 'https://feather.openai.com/api/graphql'", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"downloaded": 2}',
                stderr="",
            )
            with patch.object(review_task_slides.subprocess, "run", return_value=completed) as run:
                result = review_task_slides.run_downloader(
                    "task-1",
                    auth_curl,
                    redirect_curl,
                    conversation_curl,
                    root / "review",
                )

            command = run.call_args.args[0]
            self.assertIn("--api-original", command)
            self.assertEqual(result["download_mode"], "api_original")

    def test_manual_review_starts_background_workflow_and_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            auth_curl = root / "auth.curl.txt"
            auth_curl.write_text("saved auth", encoding="utf-8")
            captured = {}

            def fake_pipeline(args):
                captured["args"] = args
                args.status_callback({"state": "extracting", "task_id": args.task_id})
                return {
                    "state": "completed",
                    "task_id": args.task_id,
                    "output_dir": str(args.output_dir),
                }

            controller = dashboard_server.MonitorController()
            with (
                patch.object(dashboard_server, "CURL_FILE", auth_curl),
                patch.object(dashboard_server, "REVIEW_OUTPUT_ROOT", root / "content_review"),
                patch.object(dashboard_server, "LOG_FILE", root / "review.log"),
                patch.object(dashboard_server, "REDIRECT_GRAPHQL_CURL_FILE", root / "redirect.curl.txt"),
                patch.object(dashboard_server, "CONVERSATION_GRAPHQL_CURL_FILE", root / "conversation.curl.txt"),
                patch.object(dashboard_server, "run_review_pipeline", side_effect=fake_pipeline),
                patch.dict("os.environ", {"FEATHER_REVIEW_COMMENTS_PER_DECK": "7"}),
            ):
                result = controller.start_review("task-123")
                controller._review_thread.join(timeout=3)

            self.assertTrue(result["started"])
            self.assertFalse(controller._review_thread.is_alive())
            self.assertEqual(controller._status["review"]["state"], "completed")
            self.assertEqual(captured["args"].task_id, "task-123")
            self.assertEqual(captured["args"].comments_per_deck, 7)

    def test_manual_review_rejects_a_second_active_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            auth_curl = root / "auth.curl.txt"
            auth_curl.write_text("saved auth", encoding="utf-8")
            entered = threading.Event()
            release = threading.Event()

            def blocking_pipeline(args):
                entered.set()
                release.wait(timeout=3)
                return {"state": "completed", "task_id": args.task_id}

            controller = dashboard_server.MonitorController()
            with (
                patch.object(dashboard_server, "CURL_FILE", auth_curl),
                patch.object(dashboard_server, "REVIEW_OUTPUT_ROOT", root / "content_review"),
                patch.object(dashboard_server, "LOG_FILE", root / "review.log"),
                patch.object(dashboard_server, "run_review_pipeline", side_effect=blocking_pipeline),
            ):
                controller.start_review("task-1")
                self.assertTrue(entered.wait(timeout=1))
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    controller.start_review("task-2")
                release.set()
                controller._review_thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
