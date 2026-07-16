from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter


BASE_URL = "https://feather.openai.com"
SEARCH_URL = f"{BASE_URL}/api/v2/tasks/search"
WHOAMI_URL = f"{BASE_URL}/api/v2/users/whoami"
CLIENT_GIT_HASH = "befa13b162c"
REQUEST_TIMEOUT_SECONDS = 6
POLL_REQUEST_TIMEOUT_SECONDS = 3
POLL_REQUEST_RETRIES = 0
SAFE_REQUEST_RETRIES = 1
SAFE_REQUEST_RETRY_DELAY_SECONDS = 0.15
MIN_POLL_INTERVAL_SECONDS = 0.1
DEFAULT_BATCH_REFRESH_INTERVAL_SECONDS = 1.0
DEFAULT_POLL_WORKERS = 16
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
    batch_refresh_interval: float = DEFAULT_BATCH_REFRESH_INTERVAL_SECONDS
    poll_workers: int = DEFAULT_POLL_WORKERS


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


def create_http_session(pool_size: int = DEFAULT_POLL_WORKERS) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=max(2, pool_size),
        pool_maxsize=max(2, pool_size),
        max_retries=0,
        pool_block=False,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def request_with_retries(
    method: str,
    url: str,
    *,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    retries: int = SAFE_REQUEST_RETRIES,
    retry_delay: float = SAFE_REQUEST_RETRY_DELAY_SECONDS,
    session: requests.Session | None = None,
    **kwargs: Any,
) -> requests.Response:
    last_error: requests.exceptions.RequestException | None = None
    request = session.request if session is not None else requests.request
    for attempt in range(retries + 1):
        try:
            return request(method, url, timeout=timeout, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(retry_delay * (attempt + 1))

    if last_error:
        raise last_error
    raise RuntimeError("request retry loop exited without a response")


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
        copied_payload["page"] = 0
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


def fetch_batch_refs(
    headers: dict[str, str],
    campaign_id: str,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    response = request_with_retries(
        "GET",
        task_batch_refs_url(campaign_id),
        headers=headers,
        session=session,
    )
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


def active_batch_refs(
    headers: dict[str, str],
    campaign_id: str,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    return [
        ref
        for ref in fetch_batch_refs(headers, campaign_id, session=session)
        if active_batch_ref(ref)
    ]


def batch_ref_name(ref: dict[str, Any]) -> str:
    return str(ref.get("name") or "")


def batch_refs_for_regex(
    headers: dict[str, str],
    campaign_id: str,
    batch_regex: str | None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    pattern = compile_batch_regex(batch_regex)
    if not pattern:
        return []
    return [
        ref
        for ref in active_batch_refs(headers, campaign_id, session=session)
        if pattern.search(batch_ref_name(ref))
    ]


def batch_refs_for_suffix(
    headers: dict[str, str],
    campaign_id: str,
    suffix: str | None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    return batch_refs_for_regex(
        headers,
        campaign_id,
        suffix_to_batch_regex(suffix),
        session=session,
    )


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
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    if not batch_name and not batch_regex:
        return []

    refs = active_batch_refs(headers, campaign_id, session=session)
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
    session: requests.Session | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str | None, dict[str, Any]]]]:
    allowed_batch_refs = batch_refs_for_filters(
        headers,
        campaign_id,
        batch_name,
        batch_regex,
        session=session,
    )
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
    batch_values = [
        nested
        for key, value in task.items()
        if "batch" in key.lower()
        for nested in nested_values(value)
    ]
    filter_values = batch_values or values
    if batch_id and batch_id not in filter_values:
        return False

    direct_filter_match = True
    if batch_name:
        needle = batch_name.lower()
        direct_filter_match = any(needle in value.lower() for value in filter_values)
    if direct_filter_match and batch_regex:
        pattern = compile_batch_regex(batch_regex)
        direct_filter_match = bool(pattern and any(pattern.search(value) for value in filter_values))

    if allowed_batch_refs:
        allowed_ids = {str(ref.get("id")) for ref in allowed_batch_refs if ref.get("id")}
        allowed_names = {str(ref.get("name")) for ref in allowed_batch_refs if ref.get("name")}
        matches_known_ref = any(value in allowed_ids or value in allowed_names for value in filter_values)
        if not matches_known_ref and not direct_filter_match:
            return False
    elif (batch_name or batch_regex) and not direct_filter_match:
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


def poll_once(
    headers: dict[str, str],
    payload: dict[str, Any],
    session: requests.Session | None = None,
) -> dict[str, Any]:
    response = request_with_retries(
        "POST",
        SEARCH_URL,
        headers=headers,
        data=json.dumps(payload, separators=(",", ":")),
        timeout=POLL_REQUEST_TIMEOUT_SECONDS,
        retries=POLL_REQUEST_RETRIES,
        session=session,
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


def claim_task(
    headers: dict[str, str],
    task_id: str,
    session: requests.Session | None = None,
) -> requests.Response:
    post = session.post if session is not None else requests.post
    response = post(
        f"{BASE_URL}/api/graphql",
        headers=headers,
        data=json.dumps(claim_payload(task_id), separators=(",", ":")),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code in (401, 403):
        raise RuntimeError(f"auth failed during claim: HTTP {response.status_code} {response.text[:160]}")
    return response


def current_user(
    headers: dict[str, str],
    session: requests.Session | None = None,
) -> dict[str, Any]:
    response = request_with_retries("GET", WHOAMI_URL, headers=headers, session=session)
    response.raise_for_status()
    return response.json().get("user", {})


def verify_task_assignment(
    headers: dict[str, str],
    campaign_id: str,
    page_size: int,
    task_id: str,
    session: requests.Session | None = None,
) -> dict[str, Any] | None:
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
    data = poll_once(headers, verify_payload, session=session)
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
    session: requests.Session | None = None,
) -> dict[str, Any] | None:
    payload = default_search_payload(campaign_id, max(page_size, 50))
    payload["workflow_statuses"] = ["in_progress"]
    payload["exclude_declined"] = False
    data = poll_once(headers, payload, session=session)
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
    session: requests.Session | None = None,
    user: dict[str, Any] | None = None,
) -> bool:
    response = claim_task(claim_headers, task_id, session=session)
    claim_succeeded = False
    definitive_result = False
    response_preview = ""
    try:
        body = response.json()
        if isinstance(body, list):
            first = body[0] if body else {}
        else:
            first = body if isinstance(body, dict) else {}
        data = first.get("data") if isinstance(first, dict) else None
        update = data.get("updateTaskStatus") if isinstance(data, dict) else None
        definitive_result = bool(isinstance(first, dict) and (isinstance(update, dict) or first.get("errors")))
        claim_succeeded = (
            response.status_code == 200
            and isinstance(update, dict)
            and update.get("id") == task_id
            and str(update.get("workflowStatus") or "").upper() == "IN_PROGRESS"
            and not first.get("errors")
        )
        if not claim_succeeded:
            response_preview = json.dumps(body, ensure_ascii=False, separators=(",", ":"))[:1200]
    except ValueError:
        response_preview = response.text[:1200]

    emit(
        f"CLAIM status={response.status_code} success={'true' if claim_succeeded else 'false'}",
        flush=True,
    )
    if response_preview:
        emit(f"CLAIM_RESPONSE {response_preview}", flush=True)
    if claim_succeeded or definitive_result:
        return claim_succeeded

    resolved_user = user if user is not None else current_user(search_headers, session=session)
    verified_task = verify_task_assignment(
        search_headers,
        campaign_id,
        page_size,
        task_id,
        session=session,
    )
    if not verified_task:
        emit("VERIFY task_not_found_after_claim", flush=True)
        return claim_succeeded

    verification = {
        "expected_user_id": resolved_user.get("id"),
        "expected_email": resolved_user.get("email"),
        "claimed_by_user_id": verified_task.get("claimed_by_user_id"),
        "claimed_by_user_email": verified_task.get("claimed_by_user_email"),
        "active_user_id": verified_task.get("active_user_id"),
        "active_user_email": verified_task.get("active_user_email"),
        "workflow_status": verified_task.get("workflow_status"),
    }
    emit("VERIFY " + json.dumps(verification, ensure_ascii=False), flush=True)
    if resolved_user.get("id") and (
        verified_task.get("claimed_by_user_id") == resolved_user.get("id")
        or verified_task.get("active_user_id") == resolved_user.get("id")
    ):
        claim_succeeded = True
    return claim_succeeded


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor and optionally claim Feather tasks.")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--curl-file", help="File containing a Feather Copy-as-cURL request.")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--interval-min", type=float, help="Target poll period lower bound, in seconds.")
    parser.add_argument("--interval-max", type=float, help="Target poll period upper bound, in seconds.")
    parser.add_argument(
        "--batch-refresh-interval",
        type=float,
        default=DEFAULT_BATCH_REFRESH_INTERVAL_SECONDS,
        help="Refresh matching batch ids in the background every N seconds.",
    )
    parser.add_argument(
        "--poll-workers",
        type=int,
        default=DEFAULT_POLL_WORKERS,
        help="Maximum concurrent task-search requests.",
    )
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
    if interval < MIN_POLL_INTERVAL_SECONDS:
        raise SystemExit(f"Use --interval >= {MIN_POLL_INTERVAL_SECONDS}.")

    min_interval = interval_min if interval_min is not None else interval
    max_interval = interval_max if interval_max is not None else min_interval
    if min_interval < MIN_POLL_INTERVAL_SECONDS:
        raise SystemExit(f"Use --interval-min >= {MIN_POLL_INTERVAL_SECONDS}.")
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
    if config.poll_workers < 1:
        raise SystemExit("Use --poll-workers >= 1.")
    if config.batch_refresh_interval < 0.5:
        raise SystemExit("Use --batch-refresh-interval >= 0.5.")
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
        status.setdefault("poll_mode", "concurrent_low_latency")
        payload = status_payload(**status)
        write_status_payload(config.status_file, payload)
        if status_callback:
            status_callback(payload)

    def stopped_status(
        allowed_refs: list[dict[str, Any]] | None = None,
        batch_search_ids: list[str] | None = None,
    ) -> int:
        emit("STOPPED", flush=True)
        update_status(
            state="stopped",
            campaign_id=config.campaign_id,
            claim=config.claim,
            batch_regex=batch_regex,
            tag_count_filter=tag_count_filter,
            active_batch_matches=len(allowed_refs or []),
            server_batch_filters=batch_search_ids or [],
        )
        return 0

    update_status(
        state="starting",
        campaign_id=config.campaign_id,
        claim=config.claim,
        batch_regex=batch_regex,
        tag_count_filter=tag_count_filter,
    )
    if stop_requested():
        return stopped_status()

    main_session = create_http_session(config.poll_workers)
    executor: concurrent.futures.ThreadPoolExecutor | None = None
    worker_local = threading.local()

    def worker_session() -> requests.Session:
        session = getattr(worker_local, "session", None)
        if session is None:
            session = create_http_session(2)
            worker_local.session = session
        return session

    try:
        curl_text = read_curl_text(config.curl_file)
        cookie, payload = request_parts_from_curl(curl_text, config.campaign_id, config.page_size)
        payload["include_tags"] = tag_count_filter is not None
        campaign_url = f"{BASE_URL}/campaigns/{config.campaign_id}?tab=tasks&tasks-tab=unclaimed"
        search_headers = build_headers(cookie, config.campaign_id, campaign_url)
        user = current_user(search_headers, session=main_session) if config.claim else {}
        if config.claim:
            in_progress_task = find_current_in_progress_task(
                search_headers,
                config.campaign_id,
                config.page_size,
                user,
                session=main_session,
            )
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
            session=main_session,
        )
        current_batch_search_ids = batch_search_id_list(search_payloads)
        fast_lane_payload: dict[str, Any] | None = None
        if config.batch_name or batch_regex:
            fast_lane_payload = dict(payload)
            fast_lane_payload.pop("task_batch_id", None)

        def emit_batch_targets(
            refs: list[dict[str, Any]],
            searches: list[tuple[str | None, dict[str, Any]]],
        ) -> None:
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
            poll_workers=min(config.poll_workers, max(1, len(search_payloads))),
            fast_lane=fast_lane_payload is not None,
            batch_refresh_interval_seconds=config.batch_refresh_interval,
        )
        if tag_count_filter is not None:
            min_label = "*" if config.tag_count_min is None else config.tag_count_min
            max_label = "*" if config.tag_count_max is None else config.tag_count_max
            emit(f"tag_count_range={min_label}..{max_label}", flush=True)

        extra_workers = 2 if fast_lane_payload is not None else 1
        worker_count = min(config.poll_workers, max(2, len(search_payloads) + extra_workers))
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="FeatherPoll",
        )
        emit(
            f"LOW_LATENCY enabled concurrent_workers={worker_count} "
            f"fast_lane={'enabled' if fast_lane_payload is not None else 'disabled'} "
            f"batch_refresh={config.batch_refresh_interval:.2f}s",
            flush=True,
        )

        def poll_search(search_payload: dict[str, Any]) -> dict[str, Any]:
            return poll_once(search_headers, search_payload, session=worker_session())

        def refresh_batch_targets() -> tuple[list[dict[str, Any]], list[tuple[str | None, dict[str, Any]]]]:
            return resolve_batch_searches(
                search_headers,
                config.campaign_id,
                payload,
                config.batch_id,
                config.batch_name,
                batch_regex,
                session=worker_session(),
            )

        seen: set[str] = set()
        last_batch_signature = tuple(current_batch_search_ids)
        refresh_future: concurrent.futures.Future[
            tuple[list[dict[str, Any]], list[tuple[str | None, dict[str, Any]]]]
        ] | None = None
        next_batch_refresh_at = time.monotonic() + config.batch_refresh_interval

        while True:
            if stop_requested():
                return stopped_status(allowed_batch_refs, current_batch_search_ids)

            if refresh_future is not None and refresh_future.done():
                try:
                    refreshed_refs, refreshed_searches = refresh_future.result()
                    allowed_batch_refs = refreshed_refs
                    search_payloads = refreshed_searches
                    current_batch_search_ids = batch_search_id_list(search_payloads)
                    batch_signature = tuple(current_batch_search_ids)
                    if batch_signature != last_batch_signature:
                        emit_batch_targets(allowed_batch_refs, search_payloads)
                        last_batch_signature = batch_signature
                except requests.exceptions.RequestException as exc:
                    emit(
                        f"BATCH_REFRESH_FAILED using_previous_batch_filters="
                        f"{len(current_batch_search_ids)} error={exc}",
                        flush=True,
                    )
                finally:
                    refresh_future = None

            cycle_started = time.perf_counter()
            now = time.strftime("%H:%M:%S")
            if (
                (config.batch_name or batch_regex)
                and refresh_future is None
                and time.monotonic() >= next_batch_refresh_at
            ):
                refresh_future = executor.submit(refresh_batch_targets)
                next_batch_refresh_at = time.monotonic() + config.batch_refresh_interval

            cycle_searches = list(search_payloads)
            if fast_lane_payload is not None:
                cycle_searches.insert(0, (None, fast_lane_payload))
            future_to_batch = {
                executor.submit(poll_search, search_payload): batch_id
                for batch_id, search_payload in cycle_searches
            }
            tasks: list[dict[str, Any]] = []
            seen_poll_task_ids: set[str] = set()
            poll_errors: list[tuple[str, requests.exceptions.RequestException]] = []

            for future in concurrent.futures.as_completed(future_to_batch):
                if stop_requested():
                    break
                batch_id = future_to_batch[future]
                try:
                    data = future.result()
                except requests.exceptions.RequestException as exc:
                    if config.once:
                        raise
                    poll_errors.append((batch_id or "all_batches", exc))
                    emit(
                        f"[{now}] {recoverable_poll_error_name(exc)} target=tasks "
                        f"batch_id={batch_id or '*'} attempts={POLL_REQUEST_RETRIES + 1} "
                        f"timeout={POLL_REQUEST_TIMEOUT_SECONDS}s error={exc}",
                        flush=True,
                    )
                    continue

                for task in data.get("tasks", []):
                    task_id = str(task.get("id") or "").strip()
                    if task_id and task_id in seen_poll_task_ids:
                        continue
                    if task_id:
                        seen_poll_task_ids.add(task_id)
                    tasks.append(task)

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
                    if not task_id or task_id in seen:
                        continue

                    seen.add(task_id)
                    detected_at = time.perf_counter()
                    claim_succeeded = False
                    dispatch_ms: float | None = None
                    claim_result_ms: float | None = None

                    if config.claim:
                        claim_headers = build_headers(
                            cookie,
                            config.campaign_id,
                            task_url(task_id),
                            task_id=task_id,
                            task_kind=config.task_kind or task.get("kind"),
                        )
                        claim_started = time.perf_counter()
                        dispatch_ms = (claim_started - detected_at) * 1000
                        claim_succeeded = print_claim_result(
                            claim_headers,
                            search_headers,
                            config.campaign_id,
                            config.page_size,
                            task_id,
                            emit=emit,
                            session=main_session,
                            user=user,
                        )
                        claim_result_ms = (time.perf_counter() - claim_started) * 1000

                    title = task_log_title(task)
                    current_tag_count = task_tag_count(task)
                    summary = batch_summary(task)
                    save_path = config.save or None
                    if batch_regex:
                        emit(
                            f"FOUND {task_id}: {title} regex={batch_regex} "
                            f"tag_count={tag_count_label(current_tag_count)} "
                            f"batch_name={task_batch_name_label(task)}",
                            flush=True,
                        )
                    else:
                        emit(f"FOUND {task_id}: {title}", flush=True)
                    if summary:
                        emit("BATCH " + json.dumps(summary, ensure_ascii=False), flush=True)
                    if dispatch_ms is not None and claim_result_ms is not None:
                        emit(
                            f"CLAIM_TIMING {task_id} dispatch_ms={dispatch_ms:.2f} "
                            f"result_ms={claim_result_ms:.2f}",
                            flush=True,
                        )

                    save_found(save_path, task, data)
                    if save_path:
                        emit(f"SAVED {save_path}", flush=True)

                    if config.claim:
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
                                claim_dispatch_ms=round(dispatch_ms or 0.0, 2),
                                claim_result_ms=round(claim_result_ms or 0.0, 2),
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
                            claim_dispatch_ms=round(dispatch_ms or 0.0, 2),
                            claim_result_ms=round(claim_result_ms or 0.0, 2),
                        )
                        emit(f"CLAIM_SUCCEEDED_STOPPING {task_id}", flush=True)
                    else:
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
                        if not config.open_task:
                            emit(f"FOUND_CONTINUING {task_id}", flush=True)
                            continue

                    emit("\a", end="", flush=True)
                    if config.open_task:
                        webbrowser.open(task_url(task_id))
                    return 0

            if stop_requested():
                return stopped_status(allowed_batch_refs, current_batch_search_ids)

            cycle_ms = (time.perf_counter() - cycle_started) * 1000
            emit(
                f"[{now}] tasks={len(tasks)} batch_searches={len(cycle_searches)} "
                f"cycle_ms={cycle_ms:.0f}",
                flush=True,
            )
            if tasks:
                emit_task_tag_counts(tasks, emit)

            poll_error_fields: dict[str, Any] = {}
            if poll_errors:
                poll_error_fields = {
                    "poll_error_count": len(poll_errors),
                    "last_error": str(poll_errors[-1][1])[:500],
                }

            if config.once:
                update_status(
                    state="finished",
                    phase="complete",
                    campaign_id=config.campaign_id,
                    claim=config.claim,
                    tag_count_filter=tag_count_filter,
                    active_batch_matches=len(allowed_batch_refs),
                    server_batch_filters=current_batch_search_ids,
                    unclaimed_count=len(tasks),
                    last_poll=now,
                    poll_cycle_ms=round(cycle_ms, 2),
                    **poll_error_fields,
                )
                return 0

            target_period = next_sleep(min_interval, max_interval)
            delay = max(0.0, target_period - (time.perf_counter() - cycle_started))
            rate_limited = any(
                getattr(getattr(exc, "response", None), "status_code", None) == 429
                for _target, exc in poll_errors
            )
            if rate_limited:
                delay = max(delay, 2.0)
                emit("RATE_LIMIT_BACKOFF 2.00s", flush=True)

            emit(f"SLEEP {delay:.2f}s target_period={target_period:.2f}s", flush=True)
            update_status(
                state="monitoring",
                phase="sleeping_after_errors" if poll_errors else "sleeping",
                campaign_id=config.campaign_id,
                claim=config.claim,
                batch_regex=batch_regex,
                tag_count_filter=tag_count_filter,
                active_batch_matches=len(allowed_batch_refs),
                server_batch_filters=current_batch_search_ids,
                unclaimed_count=len(tasks),
                next_sleep_seconds=round(delay, 2),
                target_poll_period_seconds=round(target_period, 2),
                poll_cycle_ms=round(cycle_ms, 2),
                last_poll=now,
                **poll_error_fields,
            )
            if stop_event is not None:
                if stop_event.wait(delay):
                    return stopped_status(allowed_batch_refs, current_batch_search_ids)
            elif delay:
                time.sleep(delay)
    finally:
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        main_session.close()


def recoverable_poll_error_name(exc: requests.exceptions.RequestException) -> str:
    if getattr(getattr(exc, "response", None), "status_code", None) == 429:
        return "POLL_RATE_LIMITED"
    if isinstance(exc, requests.exceptions.Timeout):
        return "POLL_TIMEOUT"
    return "POLL_REQUEST_FAILED"


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
        batch_refresh_interval=args.batch_refresh_interval,
        poll_workers=args.poll_workers,
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
