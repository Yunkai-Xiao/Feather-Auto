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
        self.assertEqual(payload["page"], 0)
        self.assertEqual(payload["page_size"], 20)
        self.assertEqual(payload["workflow_statuses"], ["unclaimed"])


if __name__ == "__main__":
    unittest.main()
