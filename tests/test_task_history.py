from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feather_auto.task_history import TaskHistoryStore


class TaskHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "task_history.json"
        self.store = TaskHistoryStore(self.path)
        self.status = {
            "history_sample_complete": True,
            "campaign_id": "campaign-1",
            "batch_regex": "Aesthetic",
            "tag_count_filter": {"max": 8},
            "total_unclaimed_count": 12,
            "matching_count": 3,
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_records_once_per_half_hour_bucket(self) -> None:
        first = datetime(2026, 7, 21, 10, 2, tzinfo=timezone.utc)

        self.assertTrue(self.store.record_status(self.status, first))
        self.assertFalse(self.store.record_status({**self.status, "matching_count": 4}, first + timedelta(minutes=20)))
        self.assertTrue(self.store.record_status({**self.status, "matching_count": 5}, first + timedelta(minutes=30)))

        records = self.store.snapshot()["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["matching_tasks"], 3)
        self.assertEqual(records[1]["matching_tasks"], 5)
        self.assertEqual(records[0]["bucket_start"], "2026-07-21T10:00:00+00:00")
        self.assertEqual(records[1]["bucket_start"], "2026-07-21T10:30:00+00:00")

    def test_keeps_distinct_filter_series_in_the_same_bucket(self) -> None:
        observed_at = datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc)

        self.assertTrue(self.store.record_status(self.status, observed_at))
        self.assertTrue(
            self.store.record_status(
                {**self.status, "tag_count_filter": {"max": 4}, "matching_count": 1},
                observed_at,
            )
        )

        records = self.store.snapshot()["records"]
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["filter_key"], records[1]["filter_key"])

    def test_ignores_incomplete_poll_samples(self) -> None:
        recorded = self.store.record_status(
            {**self.status, "history_sample_complete": False},
            datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc),
        )

        self.assertFalse(recorded)
        self.assertFalse(self.path.exists())

    def test_persists_plain_json_without_runtime_secrets(self) -> None:
        self.store.record_status(
            {**self.status, "cookie": "secret", "access_token": "secret"},
            datetime(2026, 7, 21, 10, 5, tzinfo=timezone.utc),
        )

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        serialized = json.dumps(payload)
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("access_token", serialized)
        self.assertEqual(payload["records"][0]["total_tasks"], 12)


if __name__ == "__main__":
    unittest.main()
