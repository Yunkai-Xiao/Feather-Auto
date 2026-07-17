from __future__ import annotations

import unittest

from feather_auto.dashboard_lifetime import DashboardLeaseRegistry


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DashboardLeaseRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.leases = DashboardLeaseRegistry(
            ttl_seconds=90.0,
            close_grace_seconds=5.0,
            clock=self.clock,
        )

    def test_server_does_not_close_before_a_dashboard_connects(self) -> None:
        self.clock.advance(1_000.0)
        self.assertFalse(self.leases.poll())

    def test_last_page_disconnect_closes_after_refresh_grace(self) -> None:
        self.leases.heartbeat("page-a", True)
        state = self.leases.disconnect("page-a")

        self.assertTrue(state["shutdown_pending"])
        self.clock.advance(4.9)
        self.assertFalse(self.leases.poll())
        self.clock.advance(0.1)
        self.assertTrue(self.leases.poll())
        self.assertFalse(self.leases.poll())

    def test_reconnect_during_grace_cancels_shutdown(self) -> None:
        self.leases.heartbeat("old-page", True)
        self.leases.disconnect("old-page")
        self.clock.advance(2.0)
        state = self.leases.heartbeat("refreshed-page", True)

        self.assertFalse(state["shutdown_pending"])
        self.clock.advance(10.0)
        self.assertFalse(self.leases.poll())

    def test_all_dashboard_pages_must_disconnect(self) -> None:
        self.leases.heartbeat("page-a", True)
        self.leases.heartbeat("page-b", True)
        state = self.leases.disconnect("page-a")

        self.assertEqual(state["connected_clients"], 1)
        self.clock.advance(30.0)
        self.assertFalse(self.leases.poll())
        self.leases.disconnect("page-b")
        self.clock.advance(5.0)
        self.assertTrue(self.leases.poll())

    def test_missing_disconnect_uses_heartbeat_timeout(self) -> None:
        self.leases.heartbeat("crashed-page", True)
        self.clock.advance(90.0)

        self.assertFalse(self.leases.poll())
        self.clock.advance(5.0)
        self.assertTrue(self.leases.poll())

    def test_disabled_auto_shutdown_keeps_backend_running(self) -> None:
        self.leases.heartbeat("page-a", True)
        self.leases.disconnect("page-a", auto_shutdown=False)
        self.clock.advance(1_000.0)

        self.assertFalse(self.leases.poll())
        self.assertFalse(self.leases.snapshot()["auto_shutdown"])


if __name__ == "__main__":
    unittest.main()
