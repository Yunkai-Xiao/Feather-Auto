import unittest
from unittest.mock import patch

from feather_auto.cli import cookie_value_from_curl, request_parts_from_curl


class CurlCookieParsingTests(unittest.TestCase):
    def test_reads_firefox_cookie_header(self) -> None:
        curl_text = "curl 'https://feather.openai.com/api/v2/tasks/search' -H 'Cookie: session=abc; flag=1'"

        self.assertEqual(cookie_value_from_curl(curl_text), "session=abc; flag=1")

    def test_reads_cookie_options(self) -> None:
        self.assertEqual(cookie_value_from_curl("curl https://example.test -b 'session=abc'"), "session=abc")
        self.assertEqual(
            cookie_value_from_curl('curl https://example.test --cookie "session=xyz"'),
            "session=xyz",
        )

    @patch.dict("os.environ", {}, clear=True)
    def test_request_parts_accepts_cookie_header(self) -> None:
        curl_text = """curl 'https://feather.openai.com/api/v2/tasks/search' \\
  -H 'Cookie: session=abc' \\
  --data-raw '{"campaign_id":"copied","page":4,"page_size":50}'"""

        cookie, payload = request_parts_from_curl(curl_text, "selected", 20)

        self.assertEqual(cookie, "session=abc")
        self.assertEqual(payload["campaign_id"], "selected")
        self.assertEqual(payload["_search_api"], "stagecraft")
        self.assertIsNone(payload["cursor"])
        self.assertEqual(payload["page_size"], 20)
        self.assertEqual(payload["filter"], "CLAIMABLE")
        self.assertEqual(payload["tags_search_type"], "ALL")

    @patch.dict("os.environ", {}, clear=True)
    def test_reads_stagecraft_search_variables_and_resets_cursor(self) -> None:
        curl_text = """curl 'https://feather.openai.com/api/graphql' \\
  -b 'session=abc' \\
  --data-raw '[{"operationName":"StagecraftSearch","variables":{"filter":"CLAIMABLE","pageSize":20,"campaignId":"copied","taskBatchId":"batch-1","cursor":"next-page","tagsSearchType":"ALL"},"query":"query StagecraftSearch { __typename }"}]'"""

        cookie, payload = request_parts_from_curl(curl_text, "selected", 50)

        self.assertEqual(cookie, "session=abc")
        self.assertEqual(payload["_search_api"], "stagecraft")
        self.assertEqual(payload["campaign_id"], "selected")
        self.assertEqual(payload["task_batch_id"], "batch-1")
        self.assertEqual(payload["page_size"], 50)
        self.assertIsNone(payload["cursor"])

    def test_stagecraft_request_normalizes_enum_case_defensively(self) -> None:
        from feather_auto.cli import stagecraft_search_request_payload

        request = stagecraft_search_request_payload(
            {
                "_search_api": "stagecraft",
                "campaign_id": "campaign",
                "page_size": 20,
                "tags_search_type": "any",
            }
        )[0]

        self.assertEqual(request["variables"]["tagsSearchType"], "ANY")


if __name__ == "__main__":
    unittest.main()
