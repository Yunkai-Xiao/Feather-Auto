import unittest

from feather_auto.cli import (
    task_distributions,
    task_observation_records,
    task_type_from_batch_name,
)


class TaskDistributionTests(unittest.TestCase):
    def test_task_type_uses_known_phrase_anywhere_in_batch_name(self) -> None:
        cases = {
            "[HIGH PRIORITY - BR] AESTHETIC RANKING: abc": "Aesthetic Ranking",
            "RM CROSS-RUN DERISK - DESIGN INSTRUCTIONS - 20260727": "Design Instruction",
            "prefix-template-following-run": "Template Following",
            "A/B PREFERENCES: abc": "A/B Preferences",
            "Deck outline: abc": "Deck Outlines",
            "Quiz": "Other",
        }
        for batch_name, expected in cases.items():
            with self.subTest(batch_name=batch_name):
                self.assertEqual(task_type_from_batch_name(batch_name), expected)

    def test_builds_unclaimed_and_eligible_distributions(self) -> None:
        aesthetic = {
            "id": "task-1",
            "task_batch_name": "[HIGH PRIORITY - BR] AESTHETIC RANKING: abc",
            "tags": ["one", "two"],
        }
        design = {
            "id": "task-2",
            "task_batch_name": "DESIGN INSTRUCTIONS: def",
            "tags": ["one", "two", "three"],
        }
        quiz = {
            "id": "task-3",
            "task_batch_name": "Quiz",
            "tags": None,
        }

        payload = task_distributions(
            [aesthetic, design, quiz],
            [aesthetic, design],
            total_unclaimed_count=8,
            complete=False,
            source="matched_batches",
        )

        self.assertFalse(payload["complete"])
        self.assertEqual(payload["scanned_unclaimed_count"], 3)
        self.assertEqual(payload["reported_unclaimed_count"], 8)
        self.assertEqual(payload["eligible_count"], 2)
        self.assertEqual(
            payload["tag_counts"],
            [
                {"tag_count": 2, "label": "2", "unclaimed": 1, "eligible": 1},
                {"tag_count": 3, "label": "3", "unclaimed": 1, "eligible": 1},
                {"tag_count": None, "label": "Unknown", "unclaimed": 1, "eligible": 0},
            ],
        )
        task_types = {item["label"]: item for item in payload["task_types"]}
        self.assertEqual(task_types["Aesthetic Ranking"]["unclaimed"], 1)
        self.assertEqual(task_types["Design Instruction"]["eligible"], 1)
        self.assertEqual(task_types["Other"]["unclaimed"], 1)
        self.assertEqual(task_types["Deck Outlines"]["unclaimed"], 0)

        observations = task_observation_records(
            [aesthetic, design, quiz],
            [aesthetic, design],
        )
        self.assertEqual([item["task_id"] for item in observations], ["task-1", "task-2", "task-3"])
        self.assertEqual(observations[0]["tag_count"], 2)
        self.assertTrue(observations[0]["eligible"])
        self.assertFalse(observations[2]["eligible"])


if __name__ == "__main__":
    unittest.main()
