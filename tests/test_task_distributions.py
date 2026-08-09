import unittest

from feather_auto.cli import (
    task_distributions,
    task_matches_filters,
    task_observation_records,
    task_type_from_batch_name,
)


class TaskDistributionTests(unittest.TestCase):
    def test_task_type_tag_rule_overrides_global_bounds(self) -> None:
        rules = {
            "Aesthetic Ranking": {"max": 8},
            "Template Following": {"max": 5},
        }

        def matches(batch_name: str, tag_count: int) -> bool:
            return task_matches_filters(
                {"task_batch_name": batch_name, "tags": list(range(tag_count))},
                None,
                None,
                None,
                [],
                3,
                10,
                rules,
            )

        self.assertTrue(matches("AESTHETIC RANKING: batch", 2))
        self.assertFalse(matches("AESTHETIC RANKING: batch", 9))
        self.assertTrue(matches("TEMPLATE FOLLOWING: batch", 5))
        self.assertFalse(matches("TEMPLATE FOLLOWING: batch", 6))
        self.assertTrue(matches("STYLE MATCHING: batch", 6))
        self.assertFalse(matches("STYLE MATCHING: batch", 11))

    def test_batch_regex_mapping_overrides_inference_and_fails_closed_on_conflict(self) -> None:
        rules = {
            "Aesthetic Ranking": {"batch_regex": r"SPECIAL[- ]A", "max": 8},
            "Template Following": {"batch_regex": r"SPECIAL[- ]T", "max": 5},
        }

        def matches(batch_name: str, tag_count: int, active_rules=rules) -> bool:
            return task_matches_filters(
                {"task_batch_name": batch_name, "tags": list(range(tag_count))},
                None,
                None,
                None,
                [],
                None,
                None,
                active_rules,
            )

        self.assertTrue(matches("SPECIAL-A batch with an unusual name", 8))
        self.assertFalse(matches("SPECIAL-A batch with an unusual name", 9))
        self.assertFalse(matches("TEMPLATE FOLLOWING but not mapped", 4))
        conflicting = {
            "Aesthetic Ranking": {"batch_regex": "SPECIAL", "max": 8},
            "Template Following": {"batch_regex": "SPECIAL", "max": 5},
        }
        self.assertFalse(matches("SPECIAL batch", 4, conflicting))

        regex_only_rule = {"Style Matching": {"batch_regex": "STYLE-ONLY"}}
        self.assertFalse(
            task_matches_filters(
                {"task_batch_name": "STYLE-ONLY batch", "tags": [1, 2, 3, 4]},
                None,
                None,
                None,
                [],
                None,
                3,
                regex_only_rule,
            )
        )

        mapped_distribution = task_distributions(
            [{"id": "task-x", "task_batch_name": "SPECIAL-A odd", "tags": ["one"]}],
            [],
            total_unclaimed_count=1,
            complete=True,
            source="campaign",
            tag_count_rules=rules,
        )
        types = {item["label"]: item for item in mapped_distribution["task_types"]}
        self.assertEqual(types["Aesthetic Ranking"]["unclaimed"], 1)
        self.assertNotIn("Other", types)

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
