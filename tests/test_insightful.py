import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from feather_auto import cli, insightful


class InsightfulStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = Path(self.temp_dir.name) / "storage"
        connection = sqlite3.connect(self.storage)
        try:
            connection.execute("CREATE TABLE projects (id TEXT, name TEXT)")
            connection.execute("CREATE TABLE tasks (id TEXT, name TEXT, projectId TEXT)")
            connection.execute(
                "CREATE TABLE windows (id TEXT, start INTEGER, end INTEGER, projectId TEXT, taskId TEXT)"
            )
            connection.execute(
                "INSERT INTO projects VALUES (?, ?)",
                ("project-1", "Obsidian-Les Artistes Artifacts"),
            )
            # Insightful can retain duplicate task rows for the same employee/task compound key.
            connection.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?)",
                [
                    ("task-1", "HL Grading: Writing - campaign", "project-1"),
                    ("task-1", "HL Grading: Writing - campaign", "project-1"),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_target_deduplicates_same_insightful_task(self) -> None:
        target = insightful._resolve_target(self.storage, "HL Grading: Writing")

        self.assertEqual(
            target,
            {
                "task_id": "task-1",
                "task_name": "HL Grading: Writing - campaign",
                "project_id": "project-1",
                "project_name": "Obsidian-Les Artistes Artifacts",
            },
        )

    def test_existing_other_timer_blocks_automation(self) -> None:
        connection = sqlite3.connect(self.storage)
        try:
            connection.execute(
                "INSERT INTO windows VALUES (?, ?, ?, ?, ?)",
                ("window-1", 1, 0, "other-project", "other-task"),
            )
            connection.commit()
        finally:
            connection.close()

        target = insightful._resolve_target(self.storage, "HL Grading: Writing")
        with self.assertRaisesRegex(insightful.InsightfulAutomationError, "already timing another"):
            insightful._matching_active_window(self.storage, target)


class InsightfulVisualTests(unittest.TestCase):
    def test_finds_purple_play_button_border_in_right_side_of_window(self) -> None:
        width, height = 384, 720
        pixels = bytearray(width * height * 4)
        purple = bytes((238, 94, 105, 255))  # BGRA for Insightful's #695eee.
        for x in range(324, 360):
            for y in (269, 304):
                offset = (y * width + x) * 4
                pixels[offset : offset + 4] = purple
        for y in range(269, 305):
            for x in (324, 359):
                offset = (y * width + x) * 4
                pixels[offset : offset + 4] = purple

        self.assertEqual(insightful._find_purple_play_button(width, height, bytes(pixels)), (342, 286))

    def test_start_sequence_verifies_target_timer_after_click(self) -> None:
        target = {
            "task_id": "task-1",
            "task_name": "HL Grading: Writing - campaign",
            "project_id": "project-1",
            "project_name": "Obsidian-Les Artistes Artifacts",
        }
        rect = insightful.RECT(0, 0, 384, 720)
        matching = {"window_id": "window-1", "project_id": "project-1", "task_id": "task-1"}

        with (
            patch.object(insightful, "_resolve_target", return_value=target),
            patch.object(insightful, "_matching_active_window", side_effect=[None, None, matching]),
            patch.object(insightful, "_ensure_window", return_value=123),
            patch.object(insightful, "_focus_window") as focus,
            patch.object(insightful, "_click_relative") as click,
            patch.object(insightful, "_replace_text") as replace,
            patch.object(insightful, "_capture_window", return_value=(384, 720, b"")),
            patch.object(insightful, "_find_purple_play_button", return_value=(342, 286)),
            patch.object(insightful, "_window_rect", return_value=rect),
            patch.object(insightful.time, "sleep"),
        ):
            result = insightful.start_insightful_timer(
                storage_path=Path("storage"),
                app_path=Path("Workpuls.exe"),
            )

        self.assertEqual(result["state"], "started")
        focus.assert_called_once_with(123)
        self.assertEqual(
            [call.args for call in click.call_args_list],
            [(123, 52, 66), (123, 150, 76), (123, 180, 220), (123, 160, 169), (123, 342, 286)],
        )
        self.assertEqual(
            [call.args[0] for call in replace.call_args_list],
            ["Obsidian-Les Artistes Artifacts", "HL Grading: Writing"],
        )


class InsightfulClaimHookTests(unittest.TestCase):
    def test_enabled_claim_hook_starts_configured_timer(self) -> None:
        config = cli.MonitorConfig(
            campaign_id="campaign",
            start_insightful=True,
            insightful_task_prefix="HL Grading: Writing",
        )
        expected = {"state": "started", "task_id": "insightful-task"}

        with patch.object(cli, "start_insightful_timer", return_value=expected) as start:
            result = cli.start_insightful_after_claim(config, emit=lambda *_args, **_kwargs: None)

        self.assertEqual(result, expected)
        start.assert_called_once_with("HL Grading: Writing")

    def test_timer_failure_does_not_turn_successful_claim_into_monitor_error(self) -> None:
        config = cli.MonitorConfig(campaign_id="campaign", start_insightful=True)

        with patch.object(cli, "start_insightful_timer", side_effect=RuntimeError("window unavailable")):
            result = cli.start_insightful_after_claim(config, emit=lambda *_args, **_kwargs: None)

        self.assertEqual(result, {"state": "failed", "error": "window unavailable"})


if __name__ == "__main__":
    unittest.main()
