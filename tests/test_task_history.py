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
            "claim_history_sample_complete": True,
            "campaign_id": "campaign-1",
            "batch_regex": "Aesthetic",
            "tag_count_filter": {"max": 8},
            "total_unclaimed_count": 12,
            "matching_count": 3,
            "previously_claimed_count": 1,
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sums_complete_polls_within_each_half_hour_bucket(self) -> None:
        first = datetime(2026, 7, 21, 10, 2, tzinfo=timezone.utc)

        self.assertTrue(self.store.record_status({**self.status, "last_poll": "10:02:00"}, first))
        self.assertTrue(
            self.store.record_status(
                {**self.status, "matching_count": 4, "last_poll": "10:22:00"},
                first + timedelta(minutes=20),
            )
        )
        self.assertFalse(
            self.store.record_status(
                {**self.status, "matching_count": 4, "last_poll": "10:22:00"},
                first + timedelta(minutes=20, seconds=1),
            )
        )
        self.assertTrue(
            self.store.record_status(
                {**self.status, "matching_count": 5, "last_poll": "10:32:00"},
                first + timedelta(minutes=30),
            )
        )

        records = self.store.snapshot()["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["total_tasks"], 24)
        self.assertEqual(records[0]["matching_tasks"], 7)
        self.assertEqual(records[0]["previously_claimed_tasks"], 2)
        self.assertEqual(records[0]["last_sample_id"], "10:22:00")
        self.assertEqual(records[1]["total_tasks"], 12)
        self.assertEqual(records[1]["matching_tasks"], 5)
        self.assertEqual(records[1]["previously_claimed_tasks"], 1)
        self.assertEqual(records[0]["bucket_start"], "2026-07-21T10:00:00+00:00")
        self.assertEqual(records[1]["bucket_start"], "2026-07-21T10:30:00+00:00")

    def test_transient_nonmatching_task_is_included_in_the_bucket_sum(self) -> None:
        first = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
        empty = {
            **self.status,
            "total_unclaimed_count": 0,
            "matching_count": 0,
            "previously_claimed_count": 0,
            "last_poll": "09:00:02",
        }
        ten_tag_task = {
            **self.status,
            "total_unclaimed_count": 1,
            "matching_count": 0,
            "previously_claimed_count": 0,
            "last_poll": "09:10:15",
        }

        self.assertTrue(self.store.record_status(empty, first))
        self.assertTrue(self.store.record_status(ten_tag_task, first + timedelta(minutes=10)))
        self.assertFalse(
            self.store.record_status(
                {**empty, "last_poll": "09:10:19"},
                first + timedelta(minutes=10, seconds=4),
            )
        )

        records = self.store.snapshot()["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["total_tasks"], 1)
        self.assertEqual(records[0]["matching_tasks"], 0)
        self.assertEqual(records[0]["previously_claimed_tasks"], 0)

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
        self.assertEqual(payload["aggregation"], "sum")
        self.assertEqual(payload["version"], 3)
        self.assertEqual(payload["records"][0]["total_tasks"], 12)
        self.assertEqual(payload["records"][0]["previously_claimed_tasks"], 1)

    def test_default_retention_keeps_records_forever(self) -> None:
        first = datetime(2025, 1, 1, 10, 5, tzinfo=timezone.utc)
        later = first + timedelta(days=400)

        self.assertTrue(self.store.record_status({**self.status, "last_poll": "first"}, first))
        self.assertTrue(self.store.record_status({**self.status, "last_poll": "later"}, later))

        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["retention"], "forever")
        self.assertIsNone(snapshot["retention_days"])
        self.assertEqual(len(snapshot["records"]), 2)


if __name__ == "__main__":
    unittest.main()
