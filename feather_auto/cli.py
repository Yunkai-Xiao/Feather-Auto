from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

import requests


BASE_URL = "https://feather.openai.com"
SEARCH_URL = f"{BASE_URL}/api/v2/tasks/search"
WHOAMI_URL = f"{BASE_URL}/api/v2/users/whoami"
CLIENT_GIT_HASH = "befa13b162c"

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


def batch_refs_for_suffix(headers: dict[str, str], campaign_id: str, suffix: str | None) -> list[dict[str, Any]]:
    if not suffix:
        return []
    suffix = suffix.lower()
    refs = fetch_batch_refs(headers, campaign_id)
    return [
        ref
        for ref in refs
        if str(ref.get("name", "")).lower().endswith(suffix)
        and ref.get("status") == "active"
        and not ref.get("is_archived")
    ]


def task_matches_filters(
    task: dict[str, Any],
    batch_id: str | None,
    batch_name: str | None,
    batch_suffix: str | None,
    allowed_batch_refs: list[dict[str, Any]],
) -> bool:
    if not batch_id and not batch_name and not batch_suffix and not allowed_batch_refs:
        return True

    values = list(nested_values(task))
    if batch_id and batch_id not in values:
        return False

    if batch_name:
        needle = batch_name.lower()
        if not any(needle in value.lower() for value in values):
            return False

    if allowed_batch_refs:
        allowed_ids = {str(ref.get("id")) for ref in allowed_batch_refs if ref.get("id")}
        allowed_names = {str(ref.get("name")) for ref in allowed_batch_refs if ref.get("name")}
        if not any(value in allowed_ids or value in allowed_names for value in values):
            return False
    elif batch_suffix:
        suffix = batch_suffix.lower()
        if not any(value.lower().endswith(suffix) for value in values):
            return False

    return True


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
) -> bool:
    response = claim_task(claim_headers, task_id)
    print(f"CLAIM status={response.status_code}", flush=True)
    claim_succeeded = False
    try:
        body = response.json()
        print(json.dumps(body, ensure_ascii=False, indent=2)[:2000], flush=True)
        first = body[0] if isinstance(body, list) and body else {}
        update = first.get("data", {}).get("updateTaskStatus") if isinstance(first, dict) else None
        claim_succeeded = (
            response.status_code == 200
            and isinstance(update, dict)
            and update.get("id") == task_id
            and update.get("workflowStatus") == "IN_PROGRESS"
            and not first.get("errors")
        )
    except ValueError:
        print(response.text[:2000], flush=True)

    user = current_user(search_headers)
    verified_task = verify_task_assignment(search_headers, campaign_id, page_size, task_id)
    if not verified_task:
        print("VERIFY task_not_found_after_claim", flush=True)
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
    print("VERIFY " + json.dumps(verification, ensure_ascii=False), flush=True)
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
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--once", action="store_true", help="Run one search and exit.")
    parser.add_argument("--open", action="store_true", help="Open the first matching task in the browser.")
    parser.add_argument("--claim", action="store_true", help="Claim the first matching task.")
    parser.add_argument("--batch-id", help="Only match tasks containing this batch id.")
    parser.add_argument("--batch-name", help="Only match tasks containing this batch name.")
    parser.add_argument("--batch-suffix", help="Only match active batch names ending with this suffix.")
    parser.add_argument("--save", default="last_found_task.json", help="Where to save the found task JSON. Use empty string to disable.")
    parser.add_argument("--task-kind", help="Optional x-feather-client-task-kind header for claim.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.interval < 1.0:
        raise SystemExit("Use --interval >= 1.0.")
    if args.page_size < 1:
        raise SystemExit("Use --page-size >= 1.")

    curl_text = read_curl_text(args.curl_file)
    cookie, payload = request_parts_from_curl(curl_text, args.campaign_id, args.page_size)
    campaign_url = f"{BASE_URL}/campaigns/{args.campaign_id}?tab=tasks&tasks-tab=unclaimed"
    search_headers = build_headers(cookie, args.campaign_id, campaign_url)

    allowed_batch_refs = batch_refs_for_suffix(search_headers, args.campaign_id, args.batch_suffix)
    if args.batch_suffix:
        print(f"batch_suffix={args.batch_suffix} active_matches={len(allowed_batch_refs)}", flush=True)
        for ref in allowed_batch_refs:
            print(f"BATCH_REF {ref.get('id')} {ref.get('name')}", flush=True)

    seen: set[str] = set()
    while True:
        now = time.strftime("%H:%M:%S")
        data = poll_once(search_headers, payload)
        tasks = data.get("tasks", [])
        print(f"[{now}] tasks={len(tasks)}", flush=True)

        for task in tasks:
            if not task_matches_filters(task, args.batch_id, args.batch_name, args.batch_suffix, allowed_batch_refs):
                continue

            task_id = task.get("id")
            if not task_id or task_id in seen:
                continue

            seen.add(task_id)
            title = task.get("title") or task.get("description") or "(untitled)"
            print(f"FOUND {task_id}: {title}", flush=True)
            summary = batch_summary(task)
            if summary:
                print("BATCH " + json.dumps(summary, ensure_ascii=False), flush=True)

            save_path = args.save or None
            save_found(save_path, task, data)
            if save_path:
                print(f"SAVED {save_path}", flush=True)

            if args.claim:
                claim_headers = build_headers(
                    cookie,
                    args.campaign_id,
                    task_url(task_id),
                    task_id=task_id,
                    task_kind=args.task_kind or task.get("kind"),
                )
                claim_succeeded = print_claim_result(
                    claim_headers,
                    search_headers,
                    args.campaign_id,
                    args.page_size,
                    task_id,
                )
                if not claim_succeeded:
                    print(f"CLAIM_FAILED_CONTINUING {task_id}", flush=True)
                    continue

            print("\a", end="", flush=True)
            if args.open:
                webbrowser.open(task_url(task_id))
            return 0

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        raise SystemExit(130)
