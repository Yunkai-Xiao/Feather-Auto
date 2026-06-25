from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests


BASE_URL = "https://feather.openai.com"
SEARCH_URL = f"{BASE_URL}/api/v2/tasks/search"
WHOAMI_URL = f"{BASE_URL}/api/v2/users/whoami"
CLIENT_GIT_HASH = "befa13b162c"
Emit = Callable[..., None]
StatusCallback = Callable[[dict[str, Any]], None]

UPDATE_TASK_STATUS_QUERY = """
mutation UpdateTaskStatus($taskId: UUID!, $status: TaskStatus!, $skipFormVersionIds: [UUID!]) {
  updateTaskStatus(
    taskId: $taskId
    status: $status
    skipFormVersionIds: $skipFormVersionIds
  ) {
    id
    version
    workflowStatus
    screenerResult {
      passed
      __typename
    }
    targetStatusTransitions {
      endStatus
      intent
      __typename
    }
    __typename
  }
}
""".strip()


@dataclass
class MonitorConfig:
    campaign_id: str
    curl_file: str | None = None
    interval: float = 1.0
    interval_min: float | None = None
    interval_max: float | None = None
    page_size: int = 20
    once: bool = False
    open_task: bool = False
    claim: bool = False
    batch_id: str | None = None
    batch_name: str | None = None
    batch_regex: str | None = None
    batch_suffix: str | None = None
    save: str | None = "last_found_task.json"
    status_file: str | None = None
    task_kind: str | None = None
    tag_count_min: int | None = None
    tag_count_max: int | None = None


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def setup_log_file(path: str | None) -> None:
    if not path:
        return
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = Tee(sys.stdout, handle)
    sys.stderr = Tee(sys.stderr, handle)


def status_payload(**status: Any) -> dict[str, Any]:
    return {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pid": os.getpid(),
        **status,
    }


def write_status_payload(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    status_path = Path(path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    last_error: OSError | None = None
    for attempt in range(6):
        tmp_path = status_path.with_name(f".{status_path.name}.{os.getpid()}.{attempt}.tmp")
        try:
            tmp_path.write_text(text, encoding="utf-8")
            tmp_path.replace(status_path)
            return
        except OSError as exc:
            last_error = exc
            try:
                tmp_path.unlink()
            except OSError:
                pass
            time.sleep(0.05 * (attempt + 1))

    try:
        status_path.write_text(text, encoding="utf-8")
        return
    except OSError as exc:
        last_error = exc

    if last_error:
        print(f"STATUS_WRITE_FAILED {last_error}", file=sys.stderr, flush=True)


def write_status(path: str | None, **status: Any) -> None:
    write_status_payload(path, status_payload(**status))


def read_clipboard() -> str:
    return subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
        text=True,
    )


def read_curl_text(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    return read_clipboard()


def option_value(text: str, flag: str) -> str | None:
    patterns = [
        re.escape(flag) + r"\s+\$?'((?:[^'\\]|\\.)*)'",
        re.escape(flag) + r'\s+"((?:[^"\\]|\\.)*)"',
        re.escape(flag) + r'\s+\^"((?:.|\n)*?)\^"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return match.group(1).replace('^"', '"').replace("^&", "&").replace("^$", "$")
    return None


def header_value(text: str, header_name: str) -> str | None:
    escaped = re.escape(header_name)
    patterns = [
        r"-H\s+'{}:\s*([^']*)'".format(escaped),
        r'-H\s+"{}:\s*([^"]*)"'.format(escaped),
        r'-H\s+\^"{}:\s*((?:.|\n)*?)\^"'.format(escaped),
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).replace('^"', '"').replace("^&", "&").replace("^$", "$")
    return None


def default_search_payload(campaign_id: str, page_size: int) -> dict[str, Any]:
    return {
        "page": 0,
        "page_size": page_size,
        "order_by": [],
        "campaign_id": campaign_id,
        "workflow_statuses": ["unclaimed"],
        "exclude_declined": True,
        "include_task_background_operations_summary": False,
        "include_form_content": False,
        "split": "",
        "tags": [],
        "exclude_tags": [],
        "tags_search_type": "all",
        "query": "",
        "include_tags": False,
    }


def request_parts_from_curl(curl_text: str, campaign_id: str, page_size: int) -> tuple[str, dict[str, Any]]:
    cookie = option_value(curl_text, "-b") or os.environ.get("FEATHER_COOKIE")
    if not cookie:
        raise ValueError("No Feather cookie found. Provide --curl-file or FEATHER_COOKIE.")

    raw_body = option_value(curl_text, "--data-raw")
    if not raw_body:
        return cookie, default_search_payload(campaign_id, page_size)

    try:
        copied_payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return cookie, default_search_payload(campaign_id, page_size)

    if isinstance(copied_payload, dict) and copied_payload.get("campaign_id"):
        copied_payload["campaign_id"] = campaign_id or copied_payload["campaign_id"]
        copied_payload["page_size"] = page_size
        copied_payload["workflow_statuses"] = ["unclaimed"]
        return cookie, copied_payload

    return cookie, default_search_payload(campaign_id, page_size)


def build_headers(cookie: str, campaign_id: str, referer: str, task_id: str | None = None, task_kind: str | None = None) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": BASE_URL,
        "referer": referer,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
        "x-feather-client-campaign-id": campaign_id,
        "x-feather-client-git-hash": CLIENT_GIT_HASH,
        "cookie": cookie,
    }
    if task_id:
        headers["x-feather-client-task-id"] = task_id
    if task_kind:
        headers["x-feather-client-task-kind"] = task_kind
    return headers


def task_batch_refs_url(campaign_id: str) -> str:
    return f"{BASE_URL}/api/v2/task-batches/campaign/{campaign_id}/refs?include_archived=false"


def nested_values(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from nested_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_values(item)
    elif value is not None:
        yield str(value)


def fetch_batch_refs(headers: dict[str, str], campaign_id: str) -> list[dict[str, Any]]:
    response = requests.get(task_batch_refs_url(campaign_id), headers=headers, timeout=20)
    response.raise_for_status()
    data = response.json()
    return data.get("task_batch_refs", []) if isinstance(data, dict) else []


def active_batch_ref(ref: dict[str, Any]) -> bool:
    return ref.get("status") == "active" and not ref.get("is_archived")


def suffix_to_batch_regex(suffix: str | None) -> str | None:
    suffix = (suffix or "").strip()
    if not suffix:
        return None
    return f"{re.escape(suffix)}$"


def effective_batch_regex(batch_regex: str | None, batch_suffix: str | None = None) -> str | None:
    regex = (batch_regex or "").strip()
    if regex:
        return regex
    return suffix_to_batch_regex(batch_suffix)


def compile_batch_regex(batch_regex: str | None) -> re.Pattern[str] | None:
    regex = (batch_regex or "").strip()
    if not regex:
        return None
    try:
        return re.compile(regex, re.I)
    except re.error as exc:
        raise ValueError(f"Invalid batch regex: {exc}") from exc


def active_batch_refs(headers: dict[str, str], campaign_id: str) -> list[dict[str, Any]]:
    return [ref for ref in fetch_batch_refs(headers, campaign_id) if active_batch_ref(ref)]


def batch_ref_name(ref: dict[str, Any]) -> str:
    return str(ref.get("name") or "")


def batch_refs_for_regex(headers: dict[str, str], campaign_id: str, batch_regex: str | None) -> list[dict[str, Any]]:
    pattern = compile_batch_regex(batch_regex)
    if not pattern:
        return []
    return [ref for ref in active_batch_refs(headers, campaign_id) if pattern.search(batch_ref_name(ref))]


def batch_refs_for_suffix(headers: dict[str, str], campaign_id: str, suffix: str | None) -> list[dict[str, Any]]:
    return batch_refs_for_regex(headers, campaign_id, suffix_to_batch_regex(suffix))


def batch_ref_summary(ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ref.get("id"),
        "name": ref.get("name"),
        "status": ref.get("status"),
        "is_archived": ref.get("is_archived"),
    }


def batch_refs_for_filters(
    headers: dict[str, str],
    campaign_id: str,
    batch_name: str | None,
    batch_regex: str | None,
) -> list[dict[str, Any]]:
    if not batch_name and not batch_regex:
        return []

    refs = active_batch_refs(headers, campaign_id)
    if batch_name:
        needle = batch_name.lower()
        refs = [ref for ref in refs if needle in batch_ref_name(ref).lower()]
    if batch_regex:
        pattern = compile_batch_regex(batch_regex)
        if pattern:
            refs = [ref for ref in refs if pattern.search(batch_ref_name(ref))]
    return refs


def unique_batch_ids(refs: list[dict[str, Any]]) -> list[str]:
    batch_ids: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        batch_id = str(ref.get("id") or "").strip()
        if batch_id and batch_id not in seen:
            batch_ids.append(batch_id)
            seen.add(batch_id)
    return batch_ids


def batch_id_from_payload(payload: dict[str, Any]) -> str | None:
    batch_id = str(payload.get("task_batch_id") or "").strip()
    return batch_id or None


def search_batch_ids(
    payload: dict[str, Any],
    batch_id: str | None,
    batch_name: str | None,
    batch_regex: str | None,
    allowed_batch_refs: list[dict[str, Any]],
) -> list[str] | None:
    if batch_id:
        return [batch_id]
    if batch_name or batch_regex:
        return unique_batch_ids(allowed_batch_refs)
    copied_batch_id = batch_id_from_payload(payload)
    return [copied_batch_id] if copied_batch_id else None


def batch_search_payloads(
    payload: dict[str, Any],
    batch_ids: list[str] | None,
) -> list[tuple[str | None, dict[str, Any]]]:
    if batch_ids is None:
        batch_id = batch_id_from_payload(payload)
        return [(batch_id, dict(payload))]

    payloads: list[tuple[str | None, dict[str, Any]]] = []
    for batch_id in batch_ids:
        batch_payload = dict(payload)
        batch_payload["task_batch_id"] = batch_id
        payloads.append((batch_id, batch_payload))
    return payloads


def resolve_batch_searches(
    headers: dict[str, str],
    campaign_id: str,
    payload: dict[str, Any],
    batch_id: str | None,
    batch_name: str | None,
    batch_regex: str | None,
) -> tuple[list[dict[str, Any]], list[tuple[str | None, dict[str, Any]]]]:
    allowed_batch_refs = batch_refs_for_filters(headers, campaign_id, batch_name, batch_regex)
    batch_ids = search_batch_ids(payload, batch_id, batch_name, batch_regex, allowed_batch_refs)
    return allowed_batch_refs, batch_search_payloads(payload, batch_ids)


def batch_search_id_list(search_payloads: list[tuple[str | None, dict[str, Any]]]) -> list[str]:
    return [str(batch_id) for batch_id, _payload in search_payloads if batch_id]


def task_matches_filters(
    task: dict[str, Any],
    batch_id: str | None,
    batch_name: str | None,
    batch_regex: str | None,
    allowed_batch_refs: list[dict[str, Any]],
    tag_count_min: int | None,
    tag_count_max: int | None,
) -> bool:
    if (
        not batch_id
        and not batch_name
        and not batch_regex
        and not allowed_batch_refs
        and tag_count_min is None
        and tag_count_max is None
    ):
        return True

    if tag_count_min is not None or tag_count_max is not None:
        count = task_tag_count(task)
        if count is None:
            return False
        if tag_count_min is not None and count < tag_count_min:
            return False
        if tag_count_max is not None and count > tag_count_max:
            return False

    values = list(nested_values(task))
    if batch_id and batch_id not in values:
        return False

    if batch_name and not allowed_batch_refs:
        needle = batch_name.lower()
        if not any(needle in value.lower() for value in values):
            return False

    if allowed_batch_refs:
        allowed_ids = {str(ref.get("id")) for ref in allowed_batch_refs if ref.get("id")}
        allowed_names = {str(ref.get("name")) for ref in allowed_batch_refs if ref.get("name")}
        if not any(value in allowed_ids or value in allowed_names for value in values):
            return False
    elif batch_regex:
        pattern = compile_batch_regex(batch_regex)
        if pattern and not any(pattern.search(value) for value in values):
            return False

    return True


def task_tag_count(task: dict[str, Any]) -> int | None:
    tags = task.get("tags")
    if tags is None:
        return None
    if isinstance(tags, list):
        return len(tags)
    if isinstance(tags, dict):
        return len(tags)
    return 1


def tag_count_filter_payload(min_count: int | None, max_count: int | None) -> dict[str, int] | None:
    payload = {}
    if min_count is not None:
        payload["min"] = min_count
    if max_count is not None:
        payload["max"] = max_count
    return payload or None


def tag_count_label(count: int | None) -> str:
    return "unknown" if count is None else str(count)


def task_log_title(task: dict[str, Any]) -> str:
    return str(task.get("title") or task.get("description") or "(untitled)").replace("\r", " ").replace("\n", " ")


def task_batch_name_label(task: dict[str, Any]) -> str:
    value = task.get("task_batch_name") or task.get("batch_name") or task.get("task_batch_id")
    if value is None:
        return "unknown"
    return str(value).replace("\r", " ").replace("\n", " ")


def emit_task_tag_counts(tasks: list[dict[str, Any]], emit: Emit) -> None:
    for index, task in enumerate(tasks, start=1):
        task_id = task.get("id") or "unknown"
        emit(
            f"TASK_TAGS {index}/{len(tasks)} {task_id} "
            f"tag_count={tag_count_label(task_tag_count(task))} "
            f"batch_name={task_batch_name_label(task)} title={task_log_title(task)}",
            flush=True,
        )


def batch_summary(task: dict[str, Any]) -> dict[str, Any]:
    summary = {}
    for key, value in task.items():
        if "batch" in key.lower() or key.lower() in {"kind", "ref", "reference"}:
            summary[key] = value
    return summary


def task_url(task_id: str) -> str:
    return f"{BASE_URL}/tasks/{task_id}"


def poll_once(headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        SEARCH_URL,
        headers=headers,
        data=json.dumps(payload, separators=(",", ":")),
        timeout=20,
    )
    if response.status_code in (401, 403):
        raise RuntimeError(f"auth failed: HTTP {response.status_code} {response.text[:160]}")
    response.raise_for_status()
    return response.json()


def claim_payload(task_id: str) -> list[dict[str, Any]]:
    return [
        {
            "operationName": "UpdateTaskStatus",
            "variables": {
                "taskId": task_id,
                "status": "IN_PROGRESS",
                "skipFormVersionIds": [],
            },
            "query": UPDATE_TASK_STATUS_QUERY,
        }
    ]


def claim_task(headers: dict[str, str], task_id: str) -> requests.Response:
    response = requests.post(
        f"{BASE_URL}/api/graphql",
        headers=headers,
        data=json.dumps(claim_payload(task_id), separators=(",", ":")),
        timeout=20,
    )
    if response.status_code in (401, 403):
        raise RuntimeError(f"auth failed during claim: HTTP {response.status_code} {response.text[:160]}")
    return response


def current_user(headers: dict[str, str]) -> dict[str, Any]:
    response = requests.get(WHOAMI_URL, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json().get("user", {})


def verify_task_assignment(headers: dict[str, str], campaign_id: str, page_size: int, task_id: str) -> dict[str, Any] | None:
    verify_payload = default_search_payload(campaign_id, page_size)
    verify_payload["workflow_statuses"] = [
        "unclaimed",
        "in_progress",
        "completed",
        "needs_work",
        "in_review",
        "signed_off",
        "cancelled",
        "escalated",
        "escalation_resolved",
        "fixing_done",
        "paused",
    ]
    verify_payload["exclude_declined"] = False
    verify_payload["query"] = task_id
    data = poll_once(headers, verify_payload)
    for task in data.get("tasks", []):
        if task.get("id") == task_id:
            return task
    return None


def is_current_user_task(task: dict[str, Any], user: dict[str, Any]) -> bool:
    user_id = user.get("id")
    email = user.get("email")
    return bool(
        (user_id and task.get("claimed_by_user_id") == user_id)
        or (user_id and task.get("active_user_id") == user_id)
        or (email and task.get("claimed_by_user_email") == email)
        or (email and task.get("active_user_email") == email)
    )


def find_current_in_progress_task(
    headers: dict[str, str],
    campaign_id: str,
    page_size: int,
    user: dict[str, Any],
) -> dict[str, Any] | None:
    payload = default_search_payload(campaign_id, max(page_size, 50))
    payload["workflow_statuses"] = ["in_progress"]
    payload["exclude_declined"] = False
    data = poll_once(headers, payload)
    for task in data.get("tasks", []):
        if is_current_user_task(task, user):
            return task
    return None


def block_in_progress_status(
    task: dict[str, Any],
    campaign_id: str,
    claim: bool,
    batch_regex: str | None,
    tag_count_filter: dict[str, int] | None,
) -> dict[str, Any]:
    task_id = str(task.get("id") or "")
    return {
        "state": "blocked_in_progress",
        "phase": "stopped",
        "campaign_id": campaign_id,
        "claim": claim,
        "batch_regex": batch_regex,
        "tag_count_filter": tag_count_filter,
        "task_id": task_id,
        "title": task_log_title(task),
        "batch": batch_summary(task),
        "tag_count": task_tag_count(task),
        "blocking_reason": "An in-progress task is already assigned to this account. Claiming stopped.",
        "in_progress_task_id": task_id,
        "in_progress_task_url": task_url(task_id) if task_id else None,
        "in_progress_title": task_log_title(task),
    }


def stop_for_in_progress_task(
    task: dict[str, Any],
    campaign_id: str,
    claim: bool,
    batch_regex: str | None,
    tag_count_filter: dict[str, int] | None,
    emit: Emit,
    update_status: Callable[..., None],
) -> int:
    status = block_in_progress_status(task, campaign_id, claim, batch_regex, tag_count_filter)
    task_id = status.get("in_progress_task_id") or "unknown"
    title = status.get("in_progress_title") or "(untitled)"
    emit("\a", end="", flush=True)
    emit("!!! IN_PROGRESS_TASK_BLOCKING_CLAIM !!!", flush=True)
    emit(f"IN_PROGRESS_TASK {task_id}: {title}", flush=True)
    emit("CLAIM_STOPPED existing in-progress task must be finished or released first", flush=True)
    update_status(**status)
    return 0


def save_found(path: str | None, task: dict[str, Any], response: dict[str, Any]) -> None:
    if not path:
        return
    artifact = {
        "found_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "task": task,
        "batch_summary": batch_summary(task),
        "pagination": response.get("pagination"),
        "task_counts": response.get("task_counts"),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, ensure_ascii=False, indent=2)


def print_claim_result(
    claim_headers: dict[str, str],
    search_headers: dict[str, str],
    campaign_id: str,
    page_size: int,
    task_id: str,
    emit: Emit = print,
) -> bool:
    response = claim_task(claim_headers, task_id)
    emit(f"CLAIM status={response.status_code}", flush=True)
    claim_succeeded = False
    try:
        body = response.json()
        emit(json.dumps(body, ensure_ascii=False, indent=2)[:2000], flush=True)
        first = body[0] if isinstance(body, list) and body else {}
        data = first.get("data") if isinstance(first, dict) else None
        update = data.get("updateTaskStatus") if isinstance(data, dict) else None
        claim_succeeded = (
            response.status_code == 200
            and isinstance(update, dict)
            and update.get("id") == task_id
            and update.get("workflowStatus") == "IN_PROGRESS"
            and not first.get("errors")
        )
    except ValueError:
        emit(response.text[:2000], flush=True)

    user = current_user(search_headers)
    verified_task = verify_task_assignment(search_headers, campaign_id, page_size, task_id)
    if not verified_task:
        emit("VERIFY task_not_found_after_claim", flush=True)
        return claim_succeeded

    verification = {
        "expected_user_id": user.get("id"),
        "expected_email": user.get("email"),
        "claimed_by_user_id": verified_task.get("claimed_by_user_id"),
        "claimed_by_user_email": verified_task.get("claimed_by_user_email"),
        "active_user_id": verified_task.get("active_user_id"),
        "active_user_email": verified_task.get("active_user_email"),
        "workflow_status": verified_task.get("workflow_status"),
    }
    emit("VERIFY " + json.dumps(verification, ensure_ascii=False), flush=True)
    if user.get("id") and (
        verified_task.get("claimed_by_user_id") == user.get("id")
        or verified_task.get("active_user_id") == user.get("id")
    ):
        claim_succeeded = True
    return claim_succeeded


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor and optionally claim Feather tasks.")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--curl-file", help="File containing a Feather Copy-as-cURL request.")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--interval-min", type=float, help="Random sleep lower bound, in seconds.")
    parser.add_argument("--interval-max", type=float, help="Random sleep upper bound, in seconds.")
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--once", action="store_true", help="Run one search and exit.")
    parser.add_argument("--open", action="store_true", help="Open the first matching task in the browser.")
    parser.add_argument("--claim", action="store_true", help="Claim the first matching task.")
    parser.add_argument("--batch-id", help="Only match tasks containing this batch id.")
    parser.add_argument("--batch-name", help="Only match tasks containing this batch name.")
    parser.add_argument("--batch-regex", help="Only match active batch names matching this regex.")
    parser.add_argument("--batch-suffix", help="Deprecated alias: only match active batch names ending with this suffix.")
    parser.add_argument("--tag-count", type=int, help="Shorthand for --tag-count-min N --tag-count-max N.")
    parser.add_argument("--tag-count-min", type=int, help="Only match tasks with at least this many tags.")
    parser.add_argument("--tag-count-max", type=int, help="Only match tasks with at most this many tags.")
    parser.add_argument("--save", default="last_found_task.json", help="Where to save the found task JSON. Use empty string to disable.")
    parser.add_argument("--log-file", help="Append stdout/stderr to this file.")
    parser.add_argument("--status-file", help="Continuously write current monitor status as JSON.")
    parser.add_argument("--task-kind", help="Optional x-feather-client-task-kind header for claim.")
    return parser.parse_args(argv)


def validate_interval_values(interval: float, interval_min: float | None, interval_max: float | None) -> tuple[float, float]:
    if interval < 1.0:
        raise SystemExit("Use --interval >= 1.0.")

    min_interval = interval_min if interval_min is not None else interval
    max_interval = interval_max if interval_max is not None else min_interval
    if min_interval < 1.0:
        raise SystemExit("Use --interval-min >= 1.0.")
    if max_interval < min_interval:
        raise SystemExit("Use --interval-max >= --interval-min.")
    return min_interval, max_interval


def validate_intervals(args: argparse.Namespace) -> tuple[float, float]:
    return validate_interval_values(args.interval, args.interval_min, args.interval_max)


def next_sleep(min_interval: float, max_interval: float) -> float:
    if min_interval == max_interval:
        return min_interval
    return random.uniform(min_interval, max_interval)


def run_monitor(
    config: MonitorConfig,
    stop_event: Any | None = None,
    emit: Emit = print,
    status_callback: StatusCallback | None = None,
) -> int:
    min_interval, max_interval = validate_interval_values(config.interval, config.interval_min, config.interval_max)
    if config.page_size < 1:
        raise SystemExit("Use --page-size >= 1.")
    if config.tag_count_min is not None and config.tag_count_min < 0:
        raise SystemExit("Use --tag-count-min >= 0.")
    if config.tag_count_max is not None and config.tag_count_max < 0:
        raise SystemExit("Use --tag-count-max >= 0.")
    if config.tag_count_min is not None and config.tag_count_max is not None and config.tag_count_max < config.tag_count_min:
        raise SystemExit("Use --tag-count-max >= --tag-count-min.")
    tag_count_filter = tag_count_filter_payload(config.tag_count_min, config.tag_count_max)
    batch_regex = effective_batch_regex(config.batch_regex, config.batch_suffix)
    compile_batch_regex(batch_regex)

    def stop_requested() -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    def update_status(**status: Any) -> None:
        status.setdefault("batch_regex", batch_regex)
        payload = status_payload(**status)
        write_status_payload(config.status_file, payload)
        if status_callback:
            status_callback(payload)

    update_status(
        state="starting",
        campaign_id=config.campaign_id,
        claim=config.claim,
        batch_regex=batch_regex,
        tag_count_filter=tag_count_filter,
    )

    if stop_requested():
        update_status(
            state="stopped",
            campaign_id=config.campaign_id,
            claim=config.claim,
            batch_regex=batch_regex,
            tag_count_filter=tag_count_filter,
        )
        return 0

    curl_text = read_curl_text(config.curl_file)
    cookie, payload = request_parts_from_curl(curl_text, config.campaign_id, config.page_size)
    payload["include_tags"] = True
    campaign_url = f"{BASE_URL}/campaigns/{config.campaign_id}?tab=tasks&tasks-tab=unclaimed"
    search_headers = build_headers(cookie, config.campaign_id, campaign_url)
    user = current_user(search_headers) if config.claim else {}
    if config.claim:
        in_progress_task = find_current_in_progress_task(search_headers, config.campaign_id, config.page_size, user)
        if in_progress_task:
            return stop_for_in_progress_task(
                in_progress_task,
                config.campaign_id,
                config.claim,
                batch_regex,
                tag_count_filter,
                emit,
                update_status,
            )

    allowed_batch_refs, search_payloads = resolve_batch_searches(
        search_headers,
        config.campaign_id,
        payload,
        config.batch_id,
        config.batch_name,
        batch_regex,
    )
    current_batch_search_ids = batch_search_id_list(search_payloads)

    def emit_batch_targets(refs: list[dict[str, Any]], searches: list[tuple[str | None, dict[str, Any]]]) -> None:
        search_ids = batch_search_id_list(searches)
        if config.batch_name or batch_regex:
            filters = []
            if config.batch_name:
                filters.append(f"name={config.batch_name}")
            if batch_regex:
                filters.append(f"regex={batch_regex}")
            emit(
                f"batch_filter {' '.join(filters)} active_matches={len(refs)} "
                f"server_batch_filters={len(search_ids)}",
                flush=True,
            )
            for ref in refs:
                emit(f"BATCH_REF {ref.get('id')} {ref.get('name')}", flush=True)
        elif config.batch_id:
            emit(f"batch_id={config.batch_id} server_batch_filter=enabled", flush=True)
        elif search_ids:
            emit(f"task_batch_id={search_ids[0]} server_batch_filter=from_curl", flush=True)

    emit_batch_targets(allowed_batch_refs, search_payloads)
    update_status(
        state="monitoring",
        phase="ready",
        campaign_id=config.campaign_id,
        claim=config.claim,
        batch_regex=batch_regex,
        tag_count_filter=tag_count_filter,
        active_batch_matches=len(allowed_batch_refs),
        server_batch_filters=current_batch_search_ids,
    )
    if tag_count_filter is not None:
        min_label = "*" if config.tag_count_min is None else config.tag_count_min
        max_label = "*" if config.tag_count_max is None else config.tag_count_max
        emit(f"tag_count_range={min_label}..{max_label}", flush=True)

    seen: set[str] = set()
    last_batch_signature = tuple(current_batch_search_ids)
    while True:
        if stop_requested():
            emit("STOPPED", flush=True)
            update_status(
                state="stopped",
                campaign_id=config.campaign_id,
                claim=config.claim,
                batch_regex=batch_regex,
                tag_count_filter=tag_count_filter,
                active_batch_matches=len(allowed_batch_refs),
                server_batch_filters=current_batch_search_ids,
            )
            return 0

        now = time.strftime("%H:%M:%S")
        if config.batch_name or batch_regex:
            allowed_batch_refs, search_payloads = resolve_batch_searches(
                search_headers,
                config.campaign_id,
                payload,
                config.batch_id,
                config.batch_name,
                batch_regex,
            )
            current_batch_search_ids = batch_search_id_list(search_payloads)
            batch_signature = tuple(current_batch_search_ids)
            if batch_signature != last_batch_signature:
                emit_batch_targets(allowed_batch_refs, search_payloads)
                last_batch_signature = batch_signature

        tasks: list[dict[str, Any]] = []
        response_by_task_id: dict[str, dict[str, Any]] = {}
        seen_poll_task_ids: set[str] = set()
        for _batch_id, search_payload in search_payloads:
            data = poll_once(search_headers, search_payload)
            for task in data.get("tasks", []):
                task_id = str(task.get("id") or "").strip()
                if task_id:
                    if task_id in seen_poll_task_ids:
                        continue
                    seen_poll_task_ids.add(task_id)
                    response_by_task_id[task_id] = data
                tasks.append(task)
        emit(f"[{now}] tasks={len(tasks)} batch_searches={len(search_payloads)}", flush=True)
        if tasks:
            emit_task_tag_counts(tasks, emit)
        update_status(
            state="monitoring",
            phase="polling",
            campaign_id=config.campaign_id,
            claim=config.claim,
            batch_regex=batch_regex,
            tag_count_filter=tag_count_filter,
            active_batch_matches=len(allowed_batch_refs),
            server_batch_filters=current_batch_search_ids,
            unclaimed_count=len(tasks),
            last_poll=now,
        )

        for task in tasks:
            if stop_requested():
                break

            if not task_matches_filters(
                task,
                config.batch_id,
                config.batch_name,
                batch_regex,
                allowed_batch_refs,
                config.tag_count_min,
                config.tag_count_max,
            ):
                continue

            task_id = task.get("id")
            if not task_id or task_id in seen:
                continue

            seen.add(task_id)
            title = task_log_title(task)
            current_tag_count = task_tag_count(task)
            if batch_regex:
                emit(
                    f"FOUND {task_id}: {title} regex={batch_regex} "
                    f"tag_count={tag_count_label(current_tag_count)} "
                    f"batch_name={task_batch_name_label(task)}",
                    flush=True,
                )
            else:
                emit(f"FOUND {task_id}: {title}", flush=True)
            summary = batch_summary(task)
            if summary:
                emit("BATCH " + json.dumps(summary, ensure_ascii=False), flush=True)
            if config.claim:
                in_progress_task = find_current_in_progress_task(search_headers, config.campaign_id, config.page_size, user)
                if in_progress_task:
                    return stop_for_in_progress_task(
                        in_progress_task,
                        config.campaign_id,
                        config.claim,
                        batch_regex,
                        tag_count_filter,
                        emit,
                        update_status,
                    )
            update_status(
                state="found",
                campaign_id=config.campaign_id,
                claim=config.claim,
                task_id=task_id,
                title=title,
                batch=summary,
                tag_count=current_tag_count,
                tag_count_filter=tag_count_filter,
            )

            save_path = config.save or None
            save_found(save_path, task, response_by_task_id.get(str(task_id), {"tasks": tasks}))
            if save_path:
                emit(f"SAVED {save_path}", flush=True)

            if config.claim:
                claim_headers = build_headers(
                    cookie,
                    config.campaign_id,
                    task_url(task_id),
                    task_id=task_id,
                    task_kind=config.task_kind or task.get("kind"),
                )
                claim_succeeded = print_claim_result(
                    claim_headers,
                    search_headers,
                    config.campaign_id,
                    config.page_size,
                    task_id,
                    emit=emit,
                )
                if not claim_succeeded:
                    emit(f"CLAIM_FAILED_CONTINUING {task_id}", flush=True)
                    update_status(
                        state="claim_failed_continuing",
                        campaign_id=config.campaign_id,
                        task_id=task_id,
                        title=title,
                        batch=summary,
                        tag_count=current_tag_count,
                        tag_count_filter=tag_count_filter,
                    )
                    continue
                update_status(
                    state="claimed",
                    campaign_id=config.campaign_id,
                    task_id=task_id,
                    title=title,
                    batch=summary,
                    tag_count=current_tag_count,
                    tag_count_filter=tag_count_filter,
                    saved=save_path,
                )
                emit(f"CLAIM_SUCCEEDED_STOPPING {task_id}", flush=True)

            if not config.claim and not config.open_task:
                emit(f"FOUND_CONTINUING {task_id}", flush=True)
                continue

            emit("\a", end="", flush=True)
            if config.open_task:
                webbrowser.open(task_url(task_id))
            return 0

        if stop_requested():
            emit("STOPPED", flush=True)
            update_status(
                state="stopped",
                campaign_id=config.campaign_id,
                claim=config.claim,
                batch_regex=batch_regex,
                tag_count_filter=tag_count_filter,
                active_batch_matches=len(allowed_batch_refs),
                server_batch_filters=current_batch_search_ids,
            )
            return 0

        if config.once:
            return 0
        delay = next_sleep(min_interval, max_interval)
        emit(f"SLEEP {delay:.2f}s", flush=True)
        update_status(
            state="monitoring",
            phase="sleeping",
            campaign_id=config.campaign_id,
            claim=config.claim,
            batch_regex=batch_regex,
            tag_count_filter=tag_count_filter,
            active_batch_matches=len(allowed_batch_refs),
            server_batch_filters=current_batch_search_ids,
            unclaimed_count=len(tasks),
            next_sleep_seconds=round(delay, 2),
            last_poll=now,
        )
        if stop_event is not None:
            if stop_event.wait(delay):
                emit("STOPPED", flush=True)
                update_status(
                    state="stopped",
                    campaign_id=config.campaign_id,
                    claim=config.claim,
                    batch_regex=batch_regex,
                    tag_count_filter=tag_count_filter,
                    active_batch_matches=len(allowed_batch_refs),
                    server_batch_filters=current_batch_search_ids,
                    unclaimed_count=len(tasks),
                    last_poll=now,
                )
                return 0
        else:
            time.sleep(delay)


def config_from_args(args: argparse.Namespace) -> MonitorConfig:
    tag_count_min = args.tag_count_min
    tag_count_max = args.tag_count_max
    if args.tag_count is not None:
        if tag_count_min is not None or tag_count_max is not None:
            raise SystemExit("Use either --tag-count or --tag-count-min/--tag-count-max, not both.")
        tag_count_min = args.tag_count
        tag_count_max = args.tag_count

    return MonitorConfig(
        campaign_id=args.campaign_id,
        curl_file=args.curl_file,
        interval=args.interval,
        interval_min=args.interval_min,
        interval_max=args.interval_max,
        page_size=args.page_size,
        once=args.once,
        open_task=args.open,
        claim=args.claim,
        batch_id=args.batch_id,
        batch_name=args.batch_name,
        batch_regex=args.batch_regex,
        batch_suffix=args.batch_suffix,
        save=args.save or None,
        status_file=args.status_file,
        task_kind=args.task_kind,
        tag_count_min=tag_count_min,
        tag_count_max=tag_count_max,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_log_file(args.log_file)
    return run_monitor(config_from_args(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        raise SystemExit(130)
