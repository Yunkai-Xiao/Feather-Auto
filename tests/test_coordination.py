import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from feather_auto.coordination import (
    CoordinationConfig,
    CoordinationLease,
    LeaseUnavailable,
    release_saved_lease,
)
from feather_auto.coordination_server import LeaseStore, make_handler


class LeaseStoreTests(unittest.TestCase):
    def test_active_lease_blocks_another_operator_and_expired_lease_can_be_taken(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LeaseStore(Path(temp_dir) / "leases.sqlite3")
            first = store.acquire(
                account_key="feather:account-1",
                owner_id="owner-1",
                owner_label="Alice",
                lease_token="token-1",
                campaign_id="campaign-1",
                ttl_seconds=60,
                now=1000,
            )
            blocked = store.acquire(
                account_key="feather:account-1",
                owner_id="owner-2",
                owner_label="Bob",
                lease_token="token-2",
                campaign_id="campaign-1",
                ttl_seconds=60,
                now=1010,
            )
            takeover = store.acquire(
                account_key="feather:account-1",
                owner_id="owner-2",
                owner_label="Bob",
                lease_token="token-2",
                campaign_id="campaign-1",
                ttl_seconds=60,
                now=1061,
            )

            self.assertTrue(first["acquired"])
            self.assertFalse(blocked["acquired"])
            self.assertEqual(blocked["lease"]["owner_label"], "Alice")
            self.assertNotIn("lease_token", blocked["lease"])
            self.assertTrue(takeover["acquired"])
            self.assertEqual(takeover["lease"]["owner_label"], "Bob")

    def test_only_owner_token_can_mark_working_or_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LeaseStore(Path(temp_dir) / "leases.sqlite3")
            store.acquire(
                account_key="feather:account-1",
                owner_id="owner-1",
                owner_label="Alice",
                lease_token="token-1",
                campaign_id="campaign-1",
                ttl_seconds=60,
                now=1000,
            )

            rejected = store.mark_working(
                account_key="feather:account-1",
                lease_token="wrong-token",
                task_id="task-1",
                now=1010,
            )
            working = store.mark_working(
                account_key="feather:account-1",
                lease_token="token-1",
                task_id="task-1",
                now=1010,
            )
            wrong_release = store.release(account_key="feather:account-1", lease_token="wrong-token")
            release = store.release(account_key="feather:account-1", lease_token="token-1")

            self.assertFalse(rejected["updated"])
            self.assertTrue(working["updated"])
            self.assertEqual(working["lease"]["phase"], "working")
            self.assertEqual(working["lease"]["task_id"], "task-1")
            self.assertFalse(wrong_release["released"])
            self.assertTrue(release["released"])


class CoordinationClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token = "test-service-token-that-is-long-enough"
        store = LeaseStore(Path(self.temp_dir.name) / "leases.sqlite3")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, self.token))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def test_client_blocks_second_user_then_persists_and_releases_working_lease(self) -> None:
        state_file = Path(self.temp_dir.name) / "coordination.json"
        first = CoordinationLease(
            CoordinationConfig(
                url=self.url,
                service_token=self.token,
                owner_label="Alice",
                state_file=str(state_file),
            ),
            "feather:account-1",
            "campaign-1",
        )
        second = CoordinationLease(
            CoordinationConfig(url=self.url, service_token=self.token, owner_label="Bob"),
            "feather:account-1",
            "campaign-1",
        )

        first.acquire()
        with self.assertRaises(LeaseUnavailable) as blocked:
            second.acquire()
        self.assertEqual(blocked.exception.lease["owner_label"], "Alice")

        first.mark_working("task-1")
        first.close()
        self.assertTrue(state_file.exists())
        released = release_saved_lease(state_file, self.token)
        self.assertTrue(released["released"])
        self.assertFalse(state_file.exists())

        second.acquire()
        second.close()


if __name__ == "__main__":
    unittest.main()
