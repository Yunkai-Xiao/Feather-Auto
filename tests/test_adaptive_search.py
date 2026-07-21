import unittest
from unittest.mock import ANY, call, patch

from feather_auto.cli import MonitorConfig, run_monitor


class AdaptiveSearchTests(unittest.TestCase):
    def config(self):
        return MonitorConfig(
            campaign_id="campaign-a",
            once=True,
            batch_regex="target",
            save=None,
        )

    def batch_searches(self):
        refs = [
            {"id": "batch-a", "name": "target a"},
            {"id": "batch-b", "name": "target b"},
        ]
        searches = [
            ("batch-a", {"page": 0, "page_size": 20, "task_batch_id": "batch-a"}),
            ("batch-b", {"page": 0, "page_size": 20, "task_batch_id": "batch-b"}),
        ]
        return refs, searches

    @patch("feather_auto.cli.poll_all_pages")
    @patch("feather_auto.cli.poll_once")
    @patch("feather_auto.cli.resolve_batch_searches")
    @patch("feather_auto.cli.request_parts_from_curl")
    @patch("feather_auto.cli.read_curl_text", return_value="curl")
    @patch("feather_auto.cli.current_user", return_value={"id": "user-1", "email": "user@example.com"})
    def test_small_campaign_uses_one_complete_campaign_scan(
        self,
        _current_user,
        _read_curl,
        request_parts,
        resolve_searches,
        poll_once,
        poll_all_pages,
    ):
        base_payload = {"page": 0, "page_size": 20, "task_batch_id": "copied"}
        request_parts.return_value = ("cookie", base_payload)
        resolve_searches.return_value = self.batch_searches()
        probe = {"tasks": [], "pagination": {"page": 0, "page_size": 20, "count": 40}}
        poll_once.return_value = probe
        poll_all_pages.return_value = [probe]
        statuses = []

        self.assertEqual(
            0,
            run_monitor(
                self.config(),
                emit=lambda *_args, **_kwargs: None,
                status_callback=statuses.append,
            ),
        )

        campaign_payload = {"page": 0, "page_size": 20, "include_tags": True}
        poll_all_pages.assert_called_once_with(
            ANY,
            campaign_payload,
            first_page=probe,
            session=ANY,
        )
        sample = next(status for status in statuses if status.get("history_sample_complete"))
        self.assertEqual(sample["total_unclaimed_count"], 40)
        self.assertEqual(sample["matching_count"], 0)

    @patch("feather_auto.cli.poll_all_pages")
    @patch("feather_auto.cli.poll_once")
    @patch("feather_auto.cli.resolve_batch_searches")
    @patch("feather_auto.cli.request_parts_from_curl")
    @patch("feather_auto.cli.read_curl_text", return_value="curl")
    @patch("feather_auto.cli.current_user", return_value={"id": "user-1", "email": "user@example.com"})
    def test_four_page_campaign_uses_complete_per_batch_scans(
        self,
        _current_user,
        _read_curl,
        request_parts,
        resolve_searches,
        poll_once,
        poll_all_pages,
    ):
        base_payload = {"page": 0, "page_size": 20, "task_batch_id": "copied"}
        request_parts.return_value = ("cookie", base_payload)
        _refs, searches = self.batch_searches()
        resolve_searches.return_value = self.batch_searches()
        poll_once.return_value = {
            "tasks": [],
            "pagination": {"page": 0, "page_size": 20, "count": 61},
        }
        poll_all_pages.return_value = []

        self.assertEqual(0, run_monitor(self.config(), emit=lambda *_args, **_kwargs: None))

        self.assertEqual(
            [
                call(ANY, searches[0][1], session=ANY),
                call(ANY, searches[1][1], session=ANY),
            ],
            poll_all_pages.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
