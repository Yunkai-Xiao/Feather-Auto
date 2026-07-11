from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from argparse import Namespace
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .cli import (
    BASE_URL,
    MonitorConfig,
    active_batch_refs,
    batch_ref_summary,
    build_headers,
    compile_batch_regex,
    effective_batch_regex,
    request_parts_from_curl,
    run_monitor,
    tag_count_filter_payload,
)
from .review_task_slides import run_review_pipeline


ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
CURL_FILE = OUTPUTS / "current_feather_request.curl.txt"
REDIRECT_GRAPHQL_CURL_FILE = OUTPUTS / "current_feather_task_or_stagecraft_redirect.curl.txt"
CONVERSATION_GRAPHQL_CURL_FILE = OUTPUTS / "current_feather_conversation_widget.curl.txt"
LOG_FILE = OUTPUTS / "raw_creation_claim_monitor.log"
STATUS_FILE = OUTPUTS / "raw_creation_claim_status.json"
SAVE_FILE = OUTPUTS / "last_claimed_raw_creation_task.json"
DASHBOARD_PID_FILE = OUTPUTS / "dashboard_server.pid"
REVIEW_OUTPUT_ROOT = OUTPUTS / "content_review"
AESTHETIC_RANKING_CAMPAIGN_ID = "929712fc-fa2a-45bc-94df-2ae6d445b2ca"
CONTENT_GRADING_CAMPAIGN_ID = "c2978c67-7bc5-4fde-b4f5-330d0e001a35"
DEFAULT_CAMPAIGN_ID = AESTHETIC_RANKING_CAMPAIGN_ID
DEFAULT_REVIEW_MODE = "aesthetic_ranking"
CAMPAIGNS = [
    {
        "id": AESTHETIC_RANKING_CAMPAIGN_ID,
        "name": "Aesthetic Ranking campaign",
    },
    {
        "id": CONTENT_GRADING_CAMPAIGN_ID,
        "name": "Content Grading campaign",
    },
]
REVIEW_MODES = [
    {
        "id": "aesthetic_ranking",
        "name": "Aesthetic Ranking",
        "campaign_id": AESTHETIC_RANKING_CAMPAIGN_ID,
        "batch_regex": "Aesthetic",
        "batch_suffix": "-raw-creation",
        "tag_count_max": 8,
        "auto_review": False,
        "custom_campaign": False,
    },
    {
        "id": "content_grading",
        "name": "Content Grading",
        "campaign_id": CONTENT_GRADING_CAMPAIGN_ID,
        "batch_regex": "",
        "batch_suffix": "",
        "auto_review": True,
        "custom_campaign": False,
    },
]
ACTIVE_SESSION_TTL_SECONDS = 20.0
ACTIVE_SESSION_CHECK_SECONDS = 1.0
INACTIVE_SESSION_REASON = (
    "Dashboard heartbeat stopped. Search and claim were stopped to prevent background claims."
)


def campaign_name(campaign_id: str) -> str:
    for campaign in CAMPAIGNS:
        if campaign["id"] == campaign_id:
            return campaign["name"]
    return campaign_id


def review_mode_config(review_mode: str) -> dict[str, Any]:
    for mode in REVIEW_MODES:
        if mode["id"] == review_mode:
            return mode
    return REVIEW_MODES[0]


def dashboard_monitor_settings(config: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    review_mode = str(config.get("reviewMode") or DEFAULT_REVIEW_MODE).strip()
    mode_config = review_mode_config(review_mode)
    custom_campaign_id = str(config.get("customCampaignId") or "").strip()
    campaign_id = str(config.get("campaignId") or "").strip()
    if mode_config.get("custom_campaign"):
        campaign_id = custom_campaign_id or campaign_id or str(mode_config.get("campaign_id") or "")
        if not campaign_id:
            raise ValueError("Paste a Content Grading campaign id before starting.")
    else:
        campaign_id = campaign_id or str(mode_config.get("campaign_id") or DEFAULT_CAMPAIGN_ID)
    return review_mode, mode_config, campaign_id


def dashboard_batch_regex(config: dict[str, Any], mode_config: dict[str, Any]) -> str:
    if "batchRegex" in config and config.get("batchRegex") is not None:
        return str(config.get("batchRegex") or "").strip()
    if "batchSuffix" in config and config.get("batchSuffix") is not None:
        return effective_batch_regex(None, str(config.get("batchSuffix") or "").strip()) or ""

    regex = str(mode_config.get("batch_regex") or "").strip()
    if regex:
        return regex
    return effective_batch_regex(None, str(mode_config.get("batch_suffix") or "").strip()) or ""


def dashboard_curl_text(config: dict[str, Any]) -> str:
    curl_text = str(config.get("curlText") or "").strip()
    if curl_text:
        return curl_text
    return read_text(CURL_FILE).strip()


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


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json_body(handler: SimpleHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def optional_int(value: Any, label: str) -> int | None:
    if value in (None, ""):
        return None
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{label} must be >= 0.")
    return parsed


def dashboard_tag_count_bounds(config: dict[str, Any], mode_config: dict[str, Any]) -> tuple[int | None, int | None]:
    raw_tag_count = config.get("tagCount")
    if raw_tag_count not in (None, ""):
        explicit_min = optional_int(config.get("tagCountMin"), "Tag count min")
        explicit_max = optional_int(config.get("tagCountMax"), "Tag count max")
        if explicit_min is not None or explicit_max is not None:
            raise ValueError("Use either Tag count or a Tag count range, not both.")
        tag_count = optional_int(raw_tag_count, "Tag count")
        return tag_count, tag_count

    tag_count_min = optional_int(
        config.get("tagCountMin") if "tagCountMin" in config else mode_config.get("tag_count_min"),
        "Tag count min",
    )
    tag_count_max = optional_int(
        config.get("tagCountMax") if "tagCountMax" in config else mode_config.get("tag_count_max"),
        "Tag count max",
    )
    if tag_count_min is not None and tag_count_max is not None and tag_count_max < tag_count_min:
        raise ValueError("Tag count max must be >= tag count min.")
    return tag_count_min, tag_count_max


def tail(path: Path, max_lines: int = 260) -> str:
    text = read_text(path)
    if not text:
        return ""
    return "\n".join(text.splitlines()[-max_lines:])


def safe_task_id(value: str) -> str:
    task_id = value.strip()
    if not task_id or any(not (ch.isalnum() or ch in {"-", "_"}) for ch in task_id):
        raise ValueError("Invalid task id.")
    return task_id


def dashboard_runtime() -> dict[str, Any]:
    paddle_spec = importlib.util.find_spec("paddleocr")
    paddle_version = None
    if paddle_spec:
        try:
            paddle_version = importlib.metadata.version("paddleocr")
        except importlib.metadata.PackageNotFoundError:
            paddle_version = "unknown"
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    return {
        "python_executable": sys.executable,
        "sys_prefix": sys.prefix,
        "base_prefix": getattr(sys, "base_prefix", ""),
        "cwd": str(Path.cwd()),
        "repo_root": str(ROOT),
        "package_file": __file__,
        "paddleocr_available": bool(paddle_spec),
        "paddleocr_version": paddle_version,
        "venv_python": str(venv_python),
        "venv_python_exists": venv_python.exists(),
    }


def review_output_payload(task_id: str | None = None) -> dict[str, Any]:
    if not task_id:
        status = dashboard_state().get("status") or {}
        task_id = str((status.get("review") or {}).get("task_id") or status.get("task_id") or "").strip()
    if not task_id:
        return {
            "ok": True,
            "task_id": None,
            "output_dir": "",
            "combined_path": "",
            "combined_updated_at": None,
            "combined_markdown": "",
            "deck_files": [],
            "slide_files": [],
        }
    task_id = safe_task_id(task_id or "")
    output_dir = REVIEW_OUTPUT_ROOT / task_id
    combined_path = output_dir / "content_grading_comments.md"
    deck_dir = output_dir / "deck_reviews"
    deck_files = []
    slide_files = []
    if deck_dir.exists():
        for path in sorted(deck_dir.glob("*_content_grading_comments.md")):
            if "_slide_" in path.name:
                continue
            deck_files.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "updated_at": path.stat().st_mtime,
                    "text": read_text(path),
                }
            )
        for path in sorted(deck_dir.glob("*_slide_*_content_grading_comments.md")):
            slide_files.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "updated_at": path.stat().st_mtime,
                    "text": read_text(path),
                }
            )
    return {
        "ok": True,
        "task_id": task_id,
        "output_dir": str(output_dir),
        "combined_path": str(combined_path),
        "combined_updated_at": combined_path.stat().st_mtime if combined_path.exists() else None,
        "combined_markdown": read_text(combined_path),
        "deck_files": deck_files,
        "slide_files": slide_files,
    }


def clean_runtime_files() -> None:
    for path in [LOG_FILE, STATUS_FILE]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def git_command(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(["git", *args], 127, "", "git executable not found")


def maybe_update_from_git(argv: list[str] | None = None) -> None:
    if os.environ.get("FEATHER_AUTO_SKIP_STARTUP_UPDATE"):
        print("Startup update: skipped by FEATHER_AUTO_SKIP_STARTUP_UPDATE.", flush=True)
        return
    if os.environ.get("FEATHER_AUTO_UPDATED_ON_START"):
        return
    if git_command("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return

    branch = git_command("branch", "--show-current").stdout.strip()
    if not branch:
        print("Startup update: skipped because Git is detached.", flush=True)
        return

    upstream = git_command("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        print("Startup update: skipped because no upstream is configured.", flush=True)
        return

    dirty = git_command("status", "--porcelain")
    if dirty.stdout.strip():
        print("Startup update: skipped because local changes are present.", flush=True)
        return

    fetch = git_command("fetch", "--quiet")
    if fetch.returncode != 0:
        print(f"Startup update: fetch failed: {(fetch.stderr or fetch.stdout).strip()}", flush=True)
        return

    head = git_command("rev-parse", "HEAD").stdout.strip()
    remote = git_command("rev-parse", "@{u}").stdout.strip()
    base = git_command("merge-base", "HEAD", "@{u}").stdout.strip()
    if not head or not remote or not base or head == remote:
        print("Startup update: already up to date.", flush=True)
        return
    if base != head:
        print("Startup update: skipped because local branch has diverged.", flush=True)
        return

    pull = git_command("pull", "--ff-only", "--quiet")
    if pull.returncode != 0:
        print(f"Startup update: pull failed: {(pull.stderr or pull.stdout).strip()}", flush=True)
        return

    print(f"Startup update: pulled latest {upstream.stdout.strip()}; restarting.", flush=True)
    env = {**os.environ, "FEATHER_AUTO_UPDATED_ON_START": "1"}
    os.execve(sys.executable, [sys.executable, "-m", "feather_auto.dashboard_server", *(argv or sys.argv[1:])], env)


class MonitorController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._status: dict[str, Any] = {}
        self._last_error = ""
        self._active_session_id: str | None = None
        self._last_heartbeat_at: float | None = None
        self._allow_background_run = False
        self._stop_reason = ""

    def _running_unlocked(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _session_fields_unlocked(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        age = None
        if self._last_heartbeat_at is not None:
            age = max(0.0, now - self._last_heartbeat_at)
        return {
            "active_session_id": self._active_session_id,
            "heartbeat_ttl_seconds": ACTIVE_SESSION_TTL_SECONDS,
            "heartbeat_age_seconds": round(age, 1) if age is not None else None,
            "allow_background_run": self._allow_background_run,
            "heartbeat_required": not self._allow_background_run,
            "stop_reason": INACTIVE_SESSION_REASON if self._stop_reason == "inactive_session" else None,
        }

    def _stop_inactive_session_unlocked(self, now: float | None = None) -> bool:
        if self._allow_background_run:
            return False
        if not self._running_unlocked() or not self._active_session_id or self._last_heartbeat_at is None:
            return False
        now = time.monotonic() if now is None else now
        heartbeat_age = now - self._last_heartbeat_at
        if heartbeat_age <= ACTIVE_SESSION_TTL_SECONDS:
            return False
        self._stop_reason = "inactive_session"
        self._stop_event.set()
        self._status = {
            **self._status,
            "state": "stopping_inactive",
            "phase": "waiting_for_worker_stop",
            "heartbeat_age_seconds": round(heartbeat_age, 1),
            "heartbeat_ttl_seconds": ACTIVE_SESSION_TTL_SECONDS,
            "stop_reason": INACTIVE_SESSION_REASON,
        }
        return True

    def _watch_active_session(self, session_id: str, stop_event: threading.Event) -> None:
        while not stop_event.wait(ACTIVE_SESSION_CHECK_SECONDS):
            stopped = False
            with self._lock:
                if self._active_session_id != session_id or not self._running_unlocked():
                    return
                stopped = self._stop_inactive_session_unlocked()
            if stopped:
                self._emit("STOPPING inactive_dashboard_session heartbeat_missing", flush=True)
                return

    def _emit(self, *values: Any, sep: str = " ", end: str = "\n", flush: bool = True) -> None:
        del flush
        text = sep.join(str(value) for value in values) + end
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with self._log_lock:
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(text)

    def _update_status(self, payload: dict[str, Any]) -> None:
        with self._lock:
            status = dict(payload)
            if status.get("state") == "stopped" and self._stop_reason == "inactive_session":
                status = {
                    **status,
                    "state": "stopped_inactive",
                    "phase": "stopped",
                    "stop_reason": INACTIVE_SESSION_REASON,
                }
            self._status = {**status, **self._session_fields_unlocked()}

    def _review_claimed_task(self, task_id: str) -> None:
        review_dir = REVIEW_OUTPUT_ROOT / task_id
        def review_status_callback(payload: dict[str, Any]) -> None:
            with self._lock:
                self._status = {**self._status, "review": payload}

        try:
            self._emit(f"REVIEW_START {task_id}", flush=True)
            result = run_review_pipeline(
                Namespace(
                    task_id=task_id,
                    curl_file=CURL_FILE,
                    redirect_graphql_curl_file=REDIRECT_GRAPHQL_CURL_FILE,
                    conversation_graphql_curl_file=CONVERSATION_GRAPHQL_CURL_FILE,
                    output_dir=review_dir,
                    model=os.environ.get("FEATHER_REVIEW_MODEL", "gpt-5.5"),
                    llm_backend=os.environ.get("FEATHER_REVIEW_LLM_BACKEND", "auto"),
                    codex_model=os.environ.get("FEATHER_REVIEW_CODEX_MODEL", os.environ.get("FEATHER_REVIEW_MODEL", "gpt-5.5")),
                    codex_workers=int(os.environ.get("FEATHER_REVIEW_CODEX_WORKERS", "3")),
                    ocr_workers=int(os.environ.get("FEATHER_REVIEW_OCR_WORKERS", "4")),
                    comments_per_deck=int(os.environ.get("FEATHER_REVIEW_COMMENTS_PER_DECK", "6")),
                    ocr_backend="paddle",
                    allow_non_paddle_ocr=False,
                    review_speed=os.environ.get("FEATHER_REVIEW_SPEED", "fast"),
                    skip_download=False,
                    no_llm=False,
                    status_callback=review_status_callback,
                )
            )
            self._emit("REVIEW_DONE " + json.dumps(result, ensure_ascii=False), flush=True)
            with self._lock:
                self._status = {**self._status, "review": result}
        except BaseException as exc:
            error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self._emit(f"REVIEW_FAILED {task_id} {error}", flush=True)
            failure = {
                "state": "failed",
                "task_id": task_id,
                "output_dir": str(review_dir),
                "error": error,
            }
            write_json_file(review_dir / "review_status.json", failure)
            (review_dir / "content_grading_comments.md").write_text(
                "# Content Grading Comments\n\n"
                f"Review failed for task `{task_id}`.\n\n"
                f"Error: {error}\n",
                encoding="utf-8",
            )
            with self._lock:
                self._status = {
                    **self._status,
                    "review": failure,
                }

    def _run(self, config: MonitorConfig, stop_event: threading.Event, auto_review: bool) -> None:
        try:
            run_monitor(
                config,
                stop_event=stop_event,
                emit=self._emit,
                status_callback=self._update_status,
            )
            stop_event.set()
            with self._lock:
                self._active_session_id = None
                self._last_heartbeat_at = None
            status = dict(self._status)
            if auto_review and status.get("state") == "claimed" and status.get("task_id"):
                self._review_claimed_task(str(status["task_id"]))
            with self._lock:
                state = self._status.get("state")
                if state not in {
                    "blocked_in_progress",
                    "claimed",
                    "stopped",
                    "stopped_inactive",
                    "stopping_inactive",
                    "error",
                }:
                    self._status = {**self._status, "state": "finished"}
        except Exception as exc:
            self._last_error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self._emit("ERROR " + self._last_error)
            with self._lock:
                self._status = {**self._status, "state": "error", "error": self._last_error}
        finally:
            with self._lock:
                self._active_session_id = None
                self._last_heartbeat_at = None

    def start(self, config: dict[str, Any]) -> dict[str, Any]:
        OUTPUTS.mkdir(parents=True, exist_ok=True)
        curl_text = str(config.get("curlText") or "").strip()
        if curl_text:
            CURL_FILE.write_text(curl_text, encoding="utf-8")
        if not CURL_FILE.exists():
            raise ValueError("Paste a Feather Copy-as-cURL before starting.")

        review_mode, mode_config, campaign_id = dashboard_monitor_settings(config)
        batch_regex = dashboard_batch_regex(config, mode_config)
        compile_batch_regex(batch_regex)
        tag_count_min, tag_count_max = dashboard_tag_count_bounds(config, mode_config)
        interval_min = float(config.get("intervalMin") or 1.2)
        interval_max = float(config.get("intervalMax") or 3.8)
        auto_review = bool(config.get("autoReview", mode_config.get("auto_review", True)))
        allow_background_run = bool(config.get("allowBackgroundRun", False))
        if interval_min < 1:
            raise ValueError("Interval min must be >= 1 second.")
        if interval_max < interval_min:
            raise ValueError("Interval max must be >= interval min.")

        monitor_config = MonitorConfig(
            campaign_id=campaign_id,
            curl_file=str(CURL_FILE),
            interval_min=interval_min,
            interval_max=interval_max,
            batch_regex=batch_regex,
            save=str(SAVE_FILE),
            status_file=str(STATUS_FILE),
            claim=bool(config.get("claim", True)),
            open_task=bool(config.get("openTask", True)),
            tag_count_min=tag_count_min,
            tag_count_max=tag_count_max,
        )

        with self._lock:
            existing = self._thread if self._running_unlocked() else None
            if existing:
                self._stop_reason = "restart"
                self._active_session_id = None
                self._last_heartbeat_at = None
                self._stop_event.set()
                self._status = {**self._status, "state": "stopping"}

        if existing:
            existing.join(timeout=5)
            with self._lock:
                if existing.is_alive():
                    raise RuntimeError("Existing monitor is still stopping. Try again in a few seconds.")

        clean_runtime_files()

        stop_event = threading.Event()
        session_id = uuid.uuid4().hex
        heartbeat_at = time.monotonic()
        thread = threading.Thread(
            target=self._run,
            args=(monitor_config, stop_event, auto_review),
            name="FeatherMonitorWorker",
            daemon=True,
        )
        with self._lock:
            self._stop_event = stop_event
            self._thread = thread
            self._last_error = ""
            self._stop_reason = ""
            self._allow_background_run = allow_background_run
            self._active_session_id = session_id
            self._last_heartbeat_at = heartbeat_at
            self._status = {
                "state": "starting",
                "review_mode": mode_config["id"],
                "review_mode_name": mode_config["name"],
                "campaign_id": campaign_id,
                "campaign_name": campaign_name(campaign_id),
                "claim": monitor_config.claim,
                "batch_regex": batch_regex,
                "tag_count_filter": tag_count_filter_payload(tag_count_min, tag_count_max),
                "auto_review": auto_review,
                "allow_background_run": allow_background_run,
                **self._session_fields_unlocked(heartbeat_at),
            }
        thread.start()
        if allow_background_run:
            self._emit("BACKGROUND_RUN enabled heartbeat_watchdog_disabled", flush=True)
        else:
            watchdog = threading.Thread(
                target=self._watch_active_session,
                args=(session_id, stop_event),
                name="FeatherMonitorHeartbeatWatchdog",
                daemon=True,
            )
            watchdog.start()
        return {
            "started": True,
            "worker_id": thread.ident,
            "session_id": session_id,
            "heartbeat_ttl_seconds": ACTIVE_SESSION_TTL_SECONDS,
            "allow_background_run": allow_background_run,
        }

    def heartbeat(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            running = self._running_unlocked()
            if (
                not running
                or not session_id
                or session_id != self._active_session_id
                or self._stop_event.is_set()
            ):
                return {"active": False, "running": running, "heartbeat_ttl_seconds": ACTIVE_SESSION_TTL_SECONDS}
            now = time.monotonic()
            self._last_heartbeat_at = now
            self._status = {**self._status, **self._session_fields_unlocked(now)}
            return {
                "active": True,
                "running": running,
                "session_id": session_id,
                "heartbeat_ttl_seconds": ACTIVE_SESSION_TTL_SECONDS,
            }

    def stop(self) -> bool:
        with self._lock:
            if not self._running_unlocked():
                return False
            self._stop_reason = "manual"
            self._active_session_id = None
            self._last_heartbeat_at = None
            self._stop_event.set()
            self._status = {**self._status, "state": "stopping"}
            return True

    def state(self) -> dict[str, Any]:
        with self._lock:
            self._stop_inactive_session_unlocked()
            running = self._running_unlocked()
            worker_id = self._thread.ident if self._thread else None
            status = {**self._status, **self._session_fields_unlocked()}
            last_error = self._last_error
            if status.get("campaign_id") and not status.get("campaign_name"):
                status = {**status, "campaign_name": campaign_name(str(status["campaign_id"]))}
            if not running and status.get("state") in {"starting", "running", "monitoring", "polling", "sleeping", "found", "stopping"}:
                status = {**status, "state": "stopped"}

        return {
            "running": running,
            "pid": None,
            "server_pid": os.getpid(),
            "worker_id": worker_id if running else None,
            "curl_saved": CURL_FILE.exists(),
            "campaigns": CAMPAIGNS,
            "review_modes": REVIEW_MODES,
            "runtime": dashboard_runtime(),
            "status": status,
            "log_tail": tail(LOG_FILE),
            "stderr_tail": last_error,
            "paths": {
                "curl": str(CURL_FILE),
                "log": str(LOG_FILE),
                "status": str(STATUS_FILE),
                "save": str(SAVE_FILE),
                "redirect_graphql_curl": str(REDIRECT_GRAPHQL_CURL_FILE),
                "conversation_graphql_curl": str(CONVERSATION_GRAPHQL_CURL_FILE),
                "review_output": str(REVIEW_OUTPUT_ROOT),
            },
        }


MONITOR = MonitorController()


def start_monitor(config: dict[str, Any]) -> dict[str, Any]:
    return MONITOR.start(config)


def stop_monitor() -> bool:
    return MONITOR.stop()


def heartbeat_monitor(session_id: str) -> dict[str, Any]:
    return MONITOR.heartbeat(session_id)


def test_batch_regex(config: dict[str, Any]) -> dict[str, Any]:
    _review_mode, mode_config, campaign_id = dashboard_monitor_settings(config)
    batch_regex = dashboard_batch_regex(config, mode_config)
    pattern = compile_batch_regex(batch_regex)
    if not pattern:
        raise ValueError("Enter a batch regex before testing.")

    curl_text = dashboard_curl_text(config)
    if not curl_text:
        raise ValueError("Paste or save a Feather Copy-as-cURL before testing batch regex.")

    cookie, _payload = request_parts_from_curl(curl_text, campaign_id, page_size=20)
    campaign_url = f"{BASE_URL}/campaigns/{campaign_id}?tab=tasks&tasks-tab=unclaimed"
    headers = build_headers(cookie, campaign_id, campaign_url)
    refs = active_batch_refs(headers, campaign_id)
    matches = [batch_ref_summary(ref) for ref in refs if pattern.search(str(ref.get("name") or ""))]
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name(campaign_id),
        "batch_regex": batch_regex,
        "active_count": len(refs),
        "match_count": len(matches),
        "matches": matches,
    }


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
        if self.path.startswith("/api/review-output"):
            query = parse_qs(urlsplit(self.path).query)
            task_id = (query.get("task_id") or [""])[0]
            write_json(self, 200, review_output_payload(task_id or None))
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
            if self.path == "/api/heartbeat":
                payload = read_json_body(self)
                session_id = str(payload.get("sessionId") or "").strip()
                write_json(self, 200, {"ok": True, **heartbeat_monitor(session_id)})
                return
            if self.path == "/api/test-batch-regex":
                payload = read_json_body(self)
                write_json(self, 200, test_batch_regex(payload))
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
            if self.path == "/api/save-graphql-curls":
                payload = read_json_body(self)
                redirect_text = str(payload.get("redirectCurlText") or "").strip()
                conversation_text = str(payload.get("conversationCurlText") or "").strip()
                if not redirect_text or not conversation_text:
                    write_json(self, 400, {"ok": False, "error": "Both GraphQL cURL templates are required."})
                    return
                OUTPUTS.mkdir(parents=True, exist_ok=True)
                REDIRECT_GRAPHQL_CURL_FILE.write_text(redirect_text, encoding="utf-8")
                CONVERSATION_GRAPHQL_CURL_FILE.write_text(conversation_text, encoding="utf-8")
                write_json(self, 200, {"ok": True, "graphql_curls_saved": True})
                return
            write_json(self, 404, {"ok": False, "error": "Unknown endpoint."})
        except Exception as exc:
            write_json(self, 500, {"ok": False, "error": str(exc)})


def main(argv: list[str] | None = None) -> int:
    maybe_update_from_git(argv)

    parser = argparse.ArgumentParser(description="Run the Feather Auto dashboard server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
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
