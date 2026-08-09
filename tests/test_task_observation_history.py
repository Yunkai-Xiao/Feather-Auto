from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feather_auto.task_history import TaskObservationStore


class TaskObservationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "observations.sqlite3"
        self.store = TaskObservationStore(self.path)
        self.base = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def status(self, poll_id: str, tasks: list[dict], *, complete: bool = True) -> dict:
        return {
            "poll_observation_id": poll_id,
            "campaign_id": "campaign-1",
            "batch_regex": "Aesthetic|raw",
            "tag_count_filter": {"max": 8},
            "task_distributions": {
                "source": "campaign" if complete else "matched_batches",
                "complete": complete,
                "reported_unclaimed_count": len(tasks),
                "scanned_unclaimed_count": len(tasks),
                "eligible_count": sum(1 for task in tasks if task["eligible"]),
            },
            "task_observations": tasks,
        }

    @staticmethod
    def task(task_id: str, tag_count: int, task_type: str, eligible: bool) -> dict:
        return {
            "task_id": task_id,
            "tag_count": tag_count,
            "task_type": task_type,
            "batch_name": f"raw {task_type}: batch",
            "eligible": eligible,
        }

    def test_retains_raw_observations_and_deduplicates_tasks_for_a_window(self) -> None:
        first_tasks = [
            self.task("task-1", 7, "Aesthetic Ranking", True),
            self.task("task-2", 10, "Complete the Deck", False),
        ]
        second_tasks = [
            self.task("task-1", 7, "Aesthetic Ranking", True),
            self.task("task-3", 4, "Other", True),
        ]
        self.assertTrue(self.store.record_status(self.status("poll-1", first_tasks), self.base))
        self.assertTrue(
            self.store.record_status(
                self.status("poll-2", second_tasks, complete=False),
                self.base + timedelta(minutes=40),
            )
        )

        one_hour = self.store.distribution(
            campaign_id="campaign-1",
            batch_regex="Aesthetic|raw",
            tag_count_max=8,
            range_minutes=60,
            observed_at=self.base + timedelta(minutes=50),
        )
        self.assertEqual(one_hour["poll_count"], 2)
        self.assertEqual(one_hour["observation_count"], 4)
        self.assertEqual(one_hour["unique_task_count"], 3)
        self.assertEqual(one_hour["eligible_count"], 2)
        self.assertEqual(one_hour["partial_poll_count"], 1)
        self.assertFalse(one_hour["complete"])
        self.assertEqual(
            {item["tag_count"]: item["unclaimed"] for item in one_hour["tag_counts"]},
            {4: 1, 7: 1, 10: 1},
        )
        types = {item["label"]: item for item in one_hour["task_types"]}
        self.assertEqual(types["Aesthetic Ranking"]["unclaimed"], 1)
        self.assertEqual(types["Complete the Deck"]["eligible"], 0)
        self.assertEqual(types["Other"]["eligible"], 1)

        aesthetic_tags = self.store.distribution(
            campaign_id="campaign-1",
            batch_regex="Aesthetic|raw",
            tag_count_max=8,
            tag_task_type="Aesthetic Ranking",
            range_minutes=60,
            observed_at=self.base + timedelta(minutes=50),
        )
        self.assertEqual(aesthetic_tags["tag_task_type"], "Aesthetic Ranking")
        self.assertEqual(aesthetic_tags["tag_unique_task_count"], 1)
        self.assertEqual(
            {item["tag_count"]: item["unclaimed"] for item in aesthetic_tags["tag_counts"]},
            {7: 1},
        )
        self.assertEqual(
            {item["label"]: item["unclaimed"] for item in aesthetic_tags["task_types"]}[
                "Complete the Deck"
            ],
            1,
        )

        last_thirty_minutes = self.store.distribution(
            campaign_id="campaign-1",
            batch_regex="Aesthetic|raw",
            tag_count_max=8,
            range_minutes=30,
            observed_at=self.base + timedelta(minutes=50),
        )
        self.assertEqual(last_thirty_minutes["poll_count"], 1)
        self.assertEqual(last_thirty_minutes["observation_count"], 2)
        self.assertEqual(last_thirty_minutes["unique_task_count"], 2)

        connection = sqlite3.connect(self.path)
        try:
            raw_count = connection.execute("SELECT COUNT(*) FROM task_observations").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(raw_count, 4)

    def test_duplicate_poll_is_not_written_twice(self) -> None:
        status = self.status(
            "poll-1",
            [self.task("task-1", 7, "Aesthetic Ranking", True)],
        )
        self.assertTrue(self.store.record_status(status, self.base))
        self.assertFalse(self.store.record_status(status, self.base + timedelta(seconds=1)))

        payload = self.store.distribution(
            campaign_id="campaign-1",
            batch_regex="Aesthetic|raw",
            tag_count_max=8,
            range_minutes=None,
            observed_at=self.base + timedelta(days=365),
        )
        self.assertEqual(payload["poll_count"], 1)
        self.assertEqual(payload["observation_count"], 1)
        self.assertEqual(payload["unique_task_count"], 1)

    def test_history_snapshot_counts_unique_task_ids_per_bucket(self) -> None:
        first_tasks = [
            {**self.task("task-1", 7, "Aesthetic Ranking", True), "previously_claimed": True},
            {**self.task("task-2", 10, "Complete the Deck", False), "previously_claimed": None},
        ]
        second_tasks = [
            {**self.task("task-1", 7, "Aesthetic Ranking", True), "previously_claimed": True},
            {**self.task("task-3", 4, "Other", True), "previously_claimed": False},
        ]
        self.store.record_status(self.status("poll-1", first_tasks), self.base)
        self.store.record_status(
            self.status("poll-2", second_tasks),
            self.base + timedelta(minutes=10),
        )

        snapshot = self.store.history_snapshot(
            interval_minutes=30,
            observed_at=self.base + timedelta(minutes=15),
        )

        self.assertEqual(snapshot["aggregation"], "unique_tasks")
        self.assertEqual(len(snapshot["records"]), 1)
        record = snapshot["records"][0]
        self.assertEqual(record["sample_count"], 2)
        self.assertEqual(record["total_tasks"], 3)
        self.assertEqual(record["matching_tasks"], 2)
        self.assertEqual(record["previously_claimed_tasks"], 1)

    def test_filter_changes_reuse_raw_campaign_history(self) -> None:
        status = self.status(
            "poll-1",
            [self.task("task-1", 7, "Aesthetic Ranking", True)],
        )
        self.store.record_status(status, self.base)

        other_filter = self.store.distribution(
            campaign_id="campaign-1",
            batch_regex="Aesthetic|raw",
            tag_count_max=4,
            range_minutes=None,
            observed_at=self.base,
        )
        self.assertEqual(other_filter["poll_count"], 1)
        self.assertEqual(other_filter["unique_task_count"], 1)
        self.assertEqual(other_filter["eligible_count"], 0)

    def test_task_type_rules_recompute_historical_eligibility(self) -> None:
        tasks = [
            self.task("task-1", 9, "Aesthetic Ranking", False),
            self.task("task-2", 6, "Template Following", False),
            self.task("task-3", 6, "Style Matching", True),
        ]
        self.store.record_status(self.status("poll-1", tasks), self.base)

        payload = self.store.distribution(
            campaign_id="campaign-1",
            batch_regex="Aesthetic|raw",
            tag_count_max=10,
            tag_count_rules={
                "Aesthetic Ranking": {"max": 8},
                "Template Following": {"max": 5},
            },
            range_minutes=None,
            observed_at=self.base,
        )

        self.assertEqual(payload["eligible_count"], 1)
        types = {item["label"]: item for item in payload["task_types"]}
        self.assertEqual(types["Aesthetic Ranking"]["eligible"], 0)
        self.assertEqual(types["Template Following"]["eligible"], 0)
        self.assertEqual(types["Style Matching"]["eligible"], 1)


if __name__ == "__main__":
    unittest.main()
