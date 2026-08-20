from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from feather_auto import __version__, cli, dashboard_server


class VersionTests(unittest.TestCase):
    def test_current_version(self) -> None:
        self.assertEqual(__version__, "0.2.0")

    def test_cli_version_flag(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.parse_args(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(output.getvalue().strip().endswith(f" {__version__}"))

    def test_dashboard_state_exposes_version(self) -> None:
        with patch.object(dashboard_server.MONITOR, "state", return_value={"running": False}):
            state = dashboard_server.dashboard_state()

        self.assertEqual(state["version"], __version__)
        self.assertFalse(state["running"])

    def test_dashboard_runtime_exposes_version(self) -> None:
        self.assertEqual(dashboard_server.dashboard_runtime()["app_version"], __version__)


if __name__ == "__main__":
    unittest.main()
