from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
PID_FILE = OUTPUTS / "raw_creation_claim_monitor.pid"
CURL_FILE = OUTPUTS / "current_feather_request.curl.txt"
LOG_FILE = OUTPUTS / "raw_creation_claim_monitor.log"
STATUS_FILE = OUTPUTS / "raw_creation_claim_status.json"
STDOUT_FILE = OUTPUTS / "raw_creation_claim_monitor.stdout.log"
STDERR_FILE = OUTPUTS / "raw_creation_claim_monitor.err.log"
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


def pid_from_pid_file() -> int | None:
    pid_text = read_text(PID_FILE).strip()
    if not pid_text.isdigit():
        return None
    pid = int(pid_text)
    try:
        os.kill(pid, 0)
    except OSError:
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        return None
    return pid


def monitor_running() -> bool:
    return pid_from_pid_file() is not None


def stop_monitor() -> bool:
    pid = pid_from_pid_file()
    if pid is None:
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    return True


def tail(path: Path, max_lines: int = 260) -> str:
    text = read_text(path)
    if not text:
        return ""
    return "\n".join(text.splitlines()[-max_lines:])


def dashboard_state() -> dict[str, Any]:
    status_text = read_text(STATUS_FILE)
    status: dict[str, Any] = {}
    if status_text:
        try:
            status = json.loads(status_text)
        except json.JSONDecodeError:
            status = {"state": "status_parse_error"}

    pid = pid_from_pid_file()
    running = pid is not None
    if not running and status.get("state") in {"starting", "running", "polling", "sleeping", "found"}:
        status = {**status, "state": "stopped"}

    return {
        "running": running,
        "pid": pid,
        "curl_saved": CURL_FILE.exists(),
        "status": status,
        "log_tail": tail(LOG_FILE),
        "stderr_tail": tail(STDERR_FILE, 80),
        "paths": {
            "curl": str(CURL_FILE),
            "log": str(LOG_FILE),
            "status": str(STATUS_FILE),
            "save": str(SAVE_FILE),
        },
    }


def clean_runtime_files() -> None:
    for path in [LOG_FILE, STATUS_FILE, STDOUT_FILE, STDERR_FILE]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def start_monitor(config: dict[str, Any]) -> dict[str, Any]:
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    curl_text = str(config.get("curlText") or "").strip()
    if curl_text:
        CURL_FILE.write_text(curl_text, encoding="utf-8")
    if not CURL_FILE.exists():
        raise ValueError("Paste a Feather Copy-as-cURL before starting.")

    if monitor_running():
        stop_monitor()

    clean_runtime_files()

    campaign_id = str(config.get("campaignId") or "929712fc-fa2a-45bc-94df-2ae6d445b2ca").strip()
    batch_suffix = str(config.get("batchSuffix") or "-raw-creation").strip()
    interval_min_value = float(config.get("intervalMin") or 1.2)
    interval_max_value = float(config.get("intervalMax") or 3.8)
    if interval_min_value < 1:
        raise ValueError("Interval min must be >= 1 second.")
    if interval_max_value < interval_min_value:
        raise ValueError("Interval max must be >= interval min.")
    interval_min = str(interval_min_value)
    interval_max = str(interval_max_value)
    claim = bool(config.get("claim", True))
    open_task = bool(config.get("openTask", True))

    args = [
        sys.executable,
        "-u",
        "-m",
        "feather_auto.cli",
        "--campaign-id",
        campaign_id,
        "--interval-min",
        interval_min,
        "--interval-max",
        interval_max,
        "--batch-suffix",
        batch_suffix,
        "--curl-file",
        str(CURL_FILE),
        "--save",
        str(SAVE_FILE),
        "--log-file",
        str(LOG_FILE),
        "--status-file",
        str(STATUS_FILE),
    ]
    if claim:
        args.append("--claim")
    if open_task:
        args.append("--open")

    stdout = STDOUT_FILE.open("a", encoding="utf-8")
    stderr = STDERR_FILE.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        args,
        cwd=str(ROOT),
        stdout=stdout,
        stderr=stderr,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    stdout.close()
    stderr.close()
    PID_FILE.write_text(str(proc.pid), encoding="ascii")
    return {"started": True, "pid": proc.pid, "args": args[3:]}


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
