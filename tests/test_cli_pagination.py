import unittest
from unittest.mock import patch

from feather_auto.cli import find_current_in_progress_task, poll_all_pages


class PollAllPagesTests(unittest.TestCase):
    def test_follows_pagination_count_without_mutating_original_payload(self) -> None:
        payload = {"page": 0, "page_size": 2, "campaign_id": "campaign"}
        responses = [
            {
                "tasks": [{"id": "1"}, {"id": "2"}],
                "pagination": {"count": 5, "page_size": 2, "random_seed": "seed-1"},
            },
            {"tasks": [{"id": "3"}, {"id": "4"}], "pagination": {"count": 5, "page_size": 2}},
            {"tasks": [{"id": "5"}], "pagination": {"count": 5, "page_size": 2}},
        ]

        with patch("feather_auto.cli.poll_once", side_effect=responses) as poll:
            result = poll_all_pages({}, payload)

        self.assertEqual(result, responses)
        self.assertEqual([call.args[1]["page"] for call in poll.call_args_list], [0, 1, 2])
        self.assertNotIn("random_seed", poll.call_args_list[0].args[1])
        self.assertEqual(poll.call_args_list[1].args[1]["random_seed"], "seed-1")
        self.assertEqual(payload["page"], 0)

    def test_stops_on_short_page_when_pagination_metadata_is_missing(self) -> None:
        responses = [
            {"tasks": [{"id": "1"}, {"id": "2"}]},
            {"tasks": [{"id": "3"}]},
        ]

        with patch("feather_auto.cli.poll_once", side_effect=responses) as poll:
            result = poll_all_pages({}, {"page": 0, "page_size": 2})

        self.assertEqual(result, responses)
        self.assertEqual(poll.call_count, 2)

    def test_in_progress_guard_checks_later_pages(self) -> None:
        responses = [
            {
                "tasks": [
                    {"id": f"other-{index}", "claimed_by_user_id": "other-user"}
                    for index in range(50)
                ],
                "pagination": {"count": 51},
            },
            {
                "tasks": [{"id": "mine", "claimed_by_user_id": "user-1"}],
                "pagination": {"count": 51},
            },
        ]

        with patch("feather_auto.cli.poll_once", side_effect=responses) as poll:
            task = find_current_in_progress_task({}, "campaign", 1, {"id": "user-1"})

        self.assertEqual(task, responses[1]["tasks"][0])
        self.assertEqual(poll.call_count, 2)


if __name__ == "__main__":
    unittest.main()
