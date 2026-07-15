import unittest
from unittest.mock import patch

from feather_auto.cli import campaign_search_payload, poll_all_pages, search_page_count


def response(page, task_ids, page_size=2, random_seed="stable-seed"):
    return {
        "tasks": [{"id": task_id} for task_id in task_ids],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "count": 5,
            "random_seed": random_seed,
        },
    }


class PaginationTests(unittest.TestCase):
    def test_fetches_until_the_last_partial_page(self):
        returned = [
            response(0, ["a", "b"]),
            response(1, ["c", "d"]),
            response(2, ["e"]),
        ]
        requests = []

        def fake_poll_once(_headers, payload):
            requests.append(dict(payload))
            return returned[len(requests) - 1]

        with patch("feather_auto.cli.poll_once", side_effect=fake_poll_once):
            pages = poll_all_pages({}, {"page": 0, "page_size": 2})

        self.assertEqual([0, 1, 2], [item["page"] for item in requests])
        self.assertEqual("stable-seed", requests[1]["random_seed"])
        self.assertEqual(5, sum(len(page["tasks"]) for page in pages))

    def test_stops_if_backend_repeats_a_full_page(self):
        repeated = response(0, ["a", "b"])

        with patch("feather_auto.cli.poll_once", return_value=repeated) as mocked:
            pages = poll_all_pages({}, {"page": 0, "page_size": 2})

        self.assertEqual(2, mocked.call_count)
        self.assertEqual(1, len(pages))

    def test_continues_from_an_already_fetched_first_page(self):
        first = response(0, ["a", "b"])
        returned = [
            response(1, ["c", "d"]),
            response(2, ["e"]),
        ]

        with patch("feather_auto.cli.poll_once", side_effect=returned) as mocked:
            pages = poll_all_pages({}, {"page": 0, "page_size": 2}, first_page=first)

        self.assertEqual(2, mocked.call_count)
        self.assertEqual([0, 1, 2], [page["pagination"]["page"] for page in pages])

    def test_does_not_request_an_empty_page_after_an_exact_full_last_page(self):
        returned = [
            response(0, ["a", "b"], page_size=2),
            response(1, ["c", "d"], page_size=2),
        ]
        for item in returned:
            item["pagination"]["count"] = 4

        with patch("feather_auto.cli.poll_once", side_effect=returned) as mocked:
            pages = poll_all_pages({}, {"page": 0, "page_size": 2})

        self.assertEqual(2, mocked.call_count)
        self.assertEqual(2, len(pages))

    def test_campaign_payload_removes_a_copied_batch_filter(self):
        payload = campaign_search_payload(
            {"page": 7, "page_size": 20, "task_batch_id": "batch-a", "random_seed": "old"}
        )

        self.assertEqual(0, payload["page"])
        self.assertNotIn("task_batch_id", payload)
        self.assertNotIn("random_seed", payload)

    def test_page_count_uses_server_count_and_page_size(self):
        data = {"pagination": {"count": 61, "page_size": 20}}

        self.assertEqual(4, search_page_count(data, {"page_size": 10}))


if __name__ == "__main__":
    unittest.main()
