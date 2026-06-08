from __future__ import annotations

import argparse
import json
import os
import threading
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .cli import MonitorConfig, run_monitor


ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
CURL_FILE = OUTPUTS / "current_feather_request.curl.txt"
LOG_FILE = OUTPUTS / "raw_creation_claim_monitor.log"
STATUS_FILE = OUTPUTS / "raw_creation_claim_status.json"
SAVE_FILE = OUTPUTS / "last_claimed_raw_creation_task.json"
DASHBOARD_PID_FILE = OUTPUTS / "dashboard_server.pid"


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return default


def write_json(handler: SimpleHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler: SimpleHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def tail(path: Path, max_lines: int = 260) -> str:
    text = read_text(path)
    if not text:
        return ""
    return "\n".join(text.splitlines()[-max_lines:])


def clean_runtime_files() -> None:
    for path in [LOG_FILE, STATUS_FILE]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


class MonitorController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._status: dict[str, Any] = {}
        self._last_error = ""

    def _running_unlocked(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _emit(self, *values: Any, sep: str = " ", end: str = "\n", flush: bool = True) -> None:
        del flush
        text = sep.join(str(value) for value in values) + end
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with self._log_lock:
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(text)

    def _update_status(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._status = dict(payload)

    def _run(self, config: MonitorConfig, stop_event: threading.Event) -> None:
        try:
            run_monitor(
                config,
                stop_event=stop_event,
                emit=self._emit,
                status_callback=self._update_status,
            )
            with self._lock:
                state = self._status.get("state")
                if state not in {"claimed", "stopped", "error"}:
                    self._status = {**self._status, "state": "finished"}
        except Exception as exc:
            self._last_error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self._emit("ERROR " + self._last_error)
            with self._lock:
                self._status = {**self._status, "state": "error", "error": self._last_error}

    def start(self, config: dict[str, Any]) -> dict[str, Any]:
        OUTPUTS.mkdir(parents=True, exist_ok=True)
        curl_text = str(config.get("curlText") or "").strip()
        if curl_text:
            CURL_FILE.write_text(curl_text, encoding="utf-8")
        if not CURL_FILE.exists():
            raise ValueError("Paste a Feather Copy-as-cURL before starting.")

        campaign_id = str(config.get("campaignId") or "929712fc-fa2a-45bc-94df-2ae6d445b2ca").strip()
        batch_suffix = str(config.get("batchSuffix") or "-raw-creation").strip()
        interval_min = float(config.get("intervalMin") or 1.2)
        interval_max = float(config.get("intervalMax") or 3.8)
        if interval_min < 1:
            raise ValueError("Interval min must be >= 1 second.")
        if interval_max < interval_min:
            raise ValueError("Interval max must be >= interval min.")

        monitor_config = MonitorConfig(
            campaign_id=campaign_id,
            curl_file=str(CURL_FILE),
            interval_min=interval_min,
            interval_max=interval_max,
            batch_suffix=batch_suffix,
            save=str(SAVE_FILE),
            status_file=str(STATUS_FILE),
            claim=bool(config.get("claim", True)),
            open_task=bool(config.get("openTask", True)),
        )

        with self._lock:
            existing = self._thread if self._running_unlocked() else None
            if existing:
                self._stop_event.set()
                self._status = {**self._status, "state": "stopping"}

        if existing:
            existing.join(timeout=5)
            with self._lock:
                if existing.is_alive():
                    raise RuntimeError("Existing monitor is still stopping. Try again in a few seconds.")

        clean_runtime_files()

        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(monitor_config, stop_event),
            name="FeatherMonitorWorker",
            daemon=True,
        )
        with self._lock:
            self._stop_event = stop_event
            self._thread = thread
            self._last_error = ""
            self._status = {
                "state": "starting",
                "campaign_id": campaign_id,
                "claim": monitor_config.claim,
                "batch_suffix": batch_suffix,
            }
        thread.start()
        return {"started": True, "worker_id": thread.ident}

    def stop(self) -> bool:
        with self._lock:
            if not self._running_unlocked():
                return False
            self._stop_event.set()
            self._status = {**self._status, "state": "stopping"}
            return True

    def state(self) -> dict[str, Any]:
        with self._lock:
            running = self._running_unlocked()
            worker_id = self._thread.ident if self._thread else None
            status = dict(self._status)
            last_error = self._last_error
            if not running and status.get("state") in {"starting", "running", "polling", "sleeping", "found", "stopping"}:
                status = {**status, "state": "stopped"}

        return {
            "running": running,
            "pid": None,
            "server_pid": os.getpid(),
            "worker_id": worker_id if running else None,
            "curl_saved": CURL_FILE.exists(),
            "status": status,
            "log_tail": tail(LOG_FILE),
            "stderr_tail": last_error,
            "paths": {
                "curl": str(CURL_FILE),
                "log": str(LOG_FILE),
                "status": str(STATUS_FILE),
                "save": str(SAVE_FILE),
            },
        }


MONITOR = MonitorController()


def start_monitor(config: dict[str, Any]) -> dict[str, Any]:
    return MONITOR.start(config)


def stop_monitor() -> bool:
    return MONITOR.stop()


def dashboard_state() -> dict[str, Any]:
    return MONITOR.state()


class DashboardHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        request_path = urlsplit(path).path
        if request_path == "/":
            request_path = "/dashboard.html"
        relative = Path(unquote(request_path.lstrip("/")))
        safe_parts = [part for part in relative.parts if part not in {"", ".", ".."}]
        return str(ROOT.joinpath(*safe_parts))

    def do_GET(self) -> None:
        if self.path.startswith("/api/state"):
            write_json(self, 200, dashboard_state())
            return
        super().do_GET()

    def do_POST(self) -> None:
        try:
            if self.path == "/api/start":
                payload = read_json_body(self)
                result = start_monitor(payload)
                write_json(self, 200, {"ok": True, **result, "state": dashboard_state()})
                return
            if self.path == "/api/stop":
                stopped = stop_monitor()
                write_json(self, 200, {"ok": True, "stopped": stopped, "state": dashboard_state()})
                return
            if self.path == "/api/save-curl":
                payload = read_json_body(self)
                curl_text = str(payload.get("curlText") or "").strip()
                if not curl_text:
                    write_json(self, 400, {"ok": False, "error": "Empty cURL text."})
                    return
                OUTPUTS.mkdir(parents=True, exist_ok=True)
                CURL_FILE.write_text(curl_text, encoding="utf-8")
                write_json(self, 200, {"ok": True, "curl_saved": True})
                return
            write_json(self, 404, {"ok": False, "error": "Unknown endpoint."})
        except Exception as exc:
            write_json(self, 500, {"ok": False, "error": str(exc)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Feather Auto dashboard server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    OUTPUTS.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    DASHBOARD_PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    print(f"Dashboard: http://{args.host}:{args.port}/dashboard.html", flush=True)
    try:
        server.serve_forever()
    finally:
        try:
            if DASHBOARD_PID_FILE.read_text(encoding="ascii").strip() == str(os.getpid()):
                DASHBOARD_PID_FILE.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
