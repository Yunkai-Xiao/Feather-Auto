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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from . import __version__
from .coordination import (
    CoordinationConfig,
    CoordinationError,
    CoordinationLease,
    LeaseUnavailable,
    account_key_for_user,
    default_owner_label,
)
from .insightful import DEFAULT_TASK_PREFIX, start_insightful_timer
from .tag_rules import (
    effective_tag_count_bounds,
    normalize_tag_count_rules,
    resolve_batch_task_type,
)


BASE_URL = "https://feather.openai.com"
SEARCH_URL = f"{BASE_URL}/api/v2/tasks/search"
GRAPHQL_URL = f"{BASE_URL}/api/graphql"
WHOAMI_URL = f"{BASE_URL}/api/v2/users/whoami"
CLIENT_GIT_HASH = "37d9a5642ed"
REQUEST_TIMEOUT_SECONDS = 10
SAFE_REQUEST_RETRIES = 2
SAFE_REQUEST_RETRY_DELAY_SECONDS = 0.75
MAX_SEARCH_PAGES = 100
DISTRIBUTION_FULL_SCAN_INTERVAL_SECONDS = 30.0
TASK_HISTORY_BATCH_SIZE = 25
Emit = Callable[..., None]
StatusCallback = Callable[[dict[str, Any]], None]

TASK_TYPE_LABELS = (
    "Aesthetic Ranking",
    "Style Matching",
    "Template Following",
    "Template Creation",
    "Content Grading",
    "Design Instruction",
    "Complete the Deck",
    "A/B Preferences",
    "Deck Outlines",
)

TASK_TYPE_PATTERNS = (
    ("Aesthetic Ranking", re.compile(r"\bAESTHETIC\s+RANKING\b", re.I)),
    ("Style Matching", re.compile(r"\bSTYLE\s+MATCHING\b", re.I)),
    ("Template Following", re.compile(r"\bTEMPLATE\s+FOLLOWING\b", re.I)),
    ("Template Creation", re.compile(r"\bTEMPLATE\s+CREATION\b", re.I)),
    ("Content Grading", re.compile(r"\bCONTENT\s+GRADING\b", re.I)),
    ("Design Instruction", re.compile(r"\bDESIGN\s+INSTRUCTIONS?\b", re.I)),
    ("Complete the Deck", re.compile(r"\bCOMPLETE\s+THE\s+DECK\b", re.I)),
    ("A/B Preferences", re.compile(r"\bA\s+B\s+PREFERENCES?\b", re.I)),
    ("Deck Outlines", re.compile(r"\bDECK\s+OUTLINES?\b", re.I)),
)

STAGECRAFT_CLAIM_STAGE_QUERY = """
mutation StagecraftClaimStage($taskId: UUID!, $stageKey: String!) {
  stagecraftClaimStage(taskId: $taskId, stageKey: $stageKey) {
    id
    __typename
  }
}
""".strip()

STAGECRAFT_SEARCH_QUERY = """
query StagecraftSearch(
  $filter: StagecraftStageSearchFilter!
  $pageSize: Int!
  $taskBatchId: UUID
  $taskBatchStatus: TaskBatchStatus
  $campaignId: UUID
  $cursor: String
  $query: String
  $stageKeys: [String!]
  $tags: [String!]
  $excludeTags: [String!]
  $tagsSearchType: StageSearchTagsSearchType
  $includeStageOptions: Boolean! = true
) {
  stagecraftSearch(
    filter: $filter
    pageSize: $pageSize
    taskBatchId: $taskBatchId
    taskBatchStatus: $taskBatchStatus
    campaignId: $campaignId
    cursor: $cursor
    query: $query
    stageKeys: $stageKeys
    tags: $tags
    excludeTags: $excludeTags
    tagsSearchType: $tagsSearchType
  ) {
    results {
      task {
        id
        title
        statusUpdatedAt
        tags
        stagecraftTaskStatus
        isTemplateTask
        taskBatchName
        __typename
      }
      stage {
        key
        label
        reopenedInfo {
          reopenedByStageLabel
          reopenedIterationCount
          reopenedAt
          __typename
        }
        __typename
      }
      previousStageLabels
      mostRecentlyDecidedStageLabel
      mostRecentlyDecidedStageOwner {
        id
        email
        __typename
      }
      claimedAt
      completedAt
      __typename
    }
    nextCursor
    stageOptions @include(if: $includeStageOptions) {
      key
      label
      __typename
    }
    __typename
  }
}
""".strip()

TASK_HISTORY_QUERY = """
query TaskHistory($taskId: UUID!) {
  taskHistory(taskId: $taskId) {
    ...TaskHistoryVersion
    __typename
  }
}

fragment TaskHistoryVersion on TaskHistoryVersion {
  version
  entries {
    __typename
    ...TaskStatusHistoryEntry
    ...TaskAction
  }
  __typename
}

fragment TaskStatusHistoryEntry on TaskStatusHistoryEntry {
  __typename
  id
  workflowStatus
  transitionReason
  createdAt
  completedIn {
    days
    hours
    minutes
    __typename
  }
  taskVersion {
    createdAt
    version
    user {
      id
      email
      isAnonymized
      __typename
    }
    __typename
  }
  user {
    id
    email
    isAnonymized
    __typename
  }
}

fragment TaskAction on TaskAction {
  __typename
  actionName
  actionStatus
  onBehalfOf {
    id
    email
    isAnonymized
    __typename
  }
  createdOrCompletedAt
  parameters
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
    tag_count_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    start_insightful: bool = False
    insightful_task_prefix: str = DEFAULT_TASK_PREFIX
    coordination_url: str | None = None
    coordination_token: str | None = None
    coordination_owner: str | None = None
    coordination_state_file: str | None = None
    coordination_search_ttl: int = 120
    coordination_working_ttl: int = 12 * 60 * 60
    _coordination_lease: CoordinationLease | None = field(default=None, init=False, repr=False)


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


def create_http_session() -> requests.Session:
    return requests.Session()


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


def cookie_value_from_curl(text: str) -> str | None:
    """Read cookies from the common browser Copy-as-cURL formats."""
    return option_value(text, "-b") or option_value(text, "--cookie") or header_value(text, "cookie")


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


def stagecraft_search_payload(
    campaign_id: str,
    page_size: int,
    copied: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the internal representation of the current cursor-based search."""
    source = copied if isinstance(copied, dict) else {}
    tags_search_type = str(
        source.get("tagsSearchType") or source.get("tags_search_type") or "ALL"
    ).upper()
    if tags_search_type not in {"ALL", "ANY"}:
        tags_search_type = "ALL"
    return {
        "_search_api": "stagecraft",
        "filter": "CLAIMABLE",
        "page_size": page_size,
        "campaign_id": campaign_id or source.get("campaignId") or source.get("campaign_id"),
        "task_batch_id": source.get("taskBatchId") or source.get("task_batch_id"),
        "task_batch_status": source.get("taskBatchStatus") or source.get("task_batch_status"),
        "cursor": None,
        "query": source.get("query") or "",
        "stage_keys": source.get("stageKeys") or source.get("stage_keys") or [],
        "tags": source.get("tags") or [],
        "exclude_tags": source.get("excludeTags") or source.get("exclude_tags") or [],
        "tags_search_type": tags_search_type,
        "include_stage_options": False,
    }


def is_stagecraft_search_payload(payload: dict[str, Any]) -> bool:
    return payload.get("_search_api") == "stagecraft"


def request_parts_from_curl(curl_text: str, campaign_id: str, page_size: int) -> tuple[str, dict[str, Any]]:
    cookie = cookie_value_from_curl(curl_text) or os.environ.get("FEATHER_COOKIE")
    if not cookie:
        raise ValueError("No Feather cookie found. Provide --curl-file or FEATHER_COOKIE.")

    raw_body = option_value(curl_text, "--data-raw")
    if not raw_body:
        return cookie, stagecraft_search_payload(campaign_id, page_size)

    try:
        copied_payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return cookie, stagecraft_search_payload(campaign_id, page_size)

    if isinstance(copied_payload, list):
        for operation in copied_payload:
            if not isinstance(operation, dict) or operation.get("operationName") != "StagecraftSearch":
                continue
            variables = operation.get("variables")
            return cookie, stagecraft_search_payload(
                campaign_id,
                page_size,
                variables if isinstance(variables, dict) else None,
            )

    if isinstance(copied_payload, dict) and copied_payload.get("campaign_id"):
        return cookie, stagecraft_search_payload(campaign_id, page_size, copied_payload)

    return cookie, stagecraft_search_payload(campaign_id, page_size)


def build_headers(
    cookie: str,
    campaign_id: str,
    referer: str,
    task_id: str | None = None,
    task_kind: str | None = None,
    *,
    client_git_hash: str | None = None,
    user_agent: str | None = None,
) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": BASE_URL,
        "referer": referer,
        "user-agent": user_agent
        or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0.0.0 Safari/537.36"
        ),
        "x-feather-client-campaign-id": campaign_id,
        "x-feather-client-git-hash": client_git_hash or CLIENT_GIT_HASH,
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
    return [ref for ref in fetch_batch_refs(headers, campaign_id, session=session) if active_batch_ref(ref)]


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
    tag_count_rules: dict[str, dict[str, Any]] | None = None,
) -> bool:
    mapped_task_type, mapping_state, _mapping_matches = resolve_batch_task_type(
        str(task.get("task_batch_name") or task.get("batch_name") or ""),
        task_type_label(task),
        tag_count_rules,
    )
    if mapping_state in {"unmapped", "ambiguous"}:
        return False
    effective_min, effective_max = effective_tag_count_bounds(
        mapped_task_type,
        tag_count_min,
        tag_count_max,
        tag_count_rules,
    )
    if (
        not batch_id
        and not batch_name
        and not batch_regex
        and not allowed_batch_refs
        and effective_min is None
        and effective_max is None
    ):
        return True

    if effective_min is not None or effective_max is not None:
        count = task_tag_count(task)
        if count is None:
            return False
        if effective_min is not None and count < effective_min:
            return False
        if effective_max is not None and count > effective_max:
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


def task_type_from_batch_name(batch_name: str | None) -> str:
    """Classify the known task type phrases embedded in Feather batch names."""
    normalized = re.sub(r"[_/\-]+", " ", str(batch_name or ""))
    for label, pattern in TASK_TYPE_PATTERNS:
        if pattern.search(normalized):
            return label
    return "Other"


def task_type_label(task: dict[str, Any]) -> str:
    return task_type_from_batch_name(
        str(task.get("task_batch_name") or task.get("batch_name") or "")
    )


def resolved_task_type_label(
    task: dict[str, Any],
    tag_count_rules: dict[str, dict[str, Any]] | None = None,
) -> str:
    task_type, _mapping_state, _matches = resolve_batch_task_type(
        str(task.get("task_batch_name") or task.get("batch_name") or ""),
        task_type_label(task),
        tag_count_rules,
    )
    return task_type


def task_distributions(
    unclaimed_tasks: list[dict[str, Any]],
    eligible_tasks: list[dict[str, Any]],
    *,
    total_unclaimed_count: int | None,
    complete: bool,
    source: str,
    tag_count_rules: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build UI-ready tag-count and batch-derived task-type distributions."""

    def increment(counts: dict[Any, int], key: Any) -> None:
        counts[key] = counts.get(key, 0) + 1

    unclaimed_tag_counts: dict[int | None, int] = {}
    eligible_tag_counts: dict[int | None, int] = {}
    unclaimed_type_counts = {label: 0 for label in TASK_TYPE_LABELS}
    eligible_type_counts = {label: 0 for label in TASK_TYPE_LABELS}

    for task in unclaimed_tasks:
        increment(unclaimed_tag_counts, task_tag_count(task))
        increment(unclaimed_type_counts, resolved_task_type_label(task, tag_count_rules))
    for task in eligible_tasks:
        increment(eligible_tag_counts, task_tag_count(task))
        increment(eligible_type_counts, resolved_task_type_label(task, tag_count_rules))

    tag_keys = sorted(
        set(unclaimed_tag_counts) | set(eligible_tag_counts),
        key=lambda value: (value is None, value if value is not None else 0),
    )
    type_labels = list(TASK_TYPE_LABELS)
    if unclaimed_type_counts.get("Other", 0) or eligible_type_counts.get("Other", 0):
        type_labels.append("Other")

    return {
        "complete": complete,
        "source": source,
        "scanned_unclaimed_count": len(unclaimed_tasks),
        "reported_unclaimed_count": total_unclaimed_count,
        "eligible_count": len(eligible_tasks),
        "tag_counts": [
            {
                "tag_count": tag_count,
                "label": "Unknown" if tag_count is None else str(tag_count),
                "unclaimed": unclaimed_tag_counts.get(tag_count, 0),
                "eligible": eligible_tag_counts.get(tag_count, 0),
            }
            for tag_count in tag_keys
        ],
        "task_types": [
            {
                "label": label,
                "unclaimed": unclaimed_type_counts.get(label, 0),
                "eligible": eligible_type_counts.get(label, 0),
            }
            for label in type_labels
        ],
    }


def task_observation_records(
    unclaimed_tasks: list[dict[str, Any]],
    eligible_tasks: list[dict[str, Any]],
    tag_count_rules: dict[str, dict[str, Any]] | None = None,
    claim_history_by_task_id: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Return the durable, non-secret task fields needed for historical distributions."""
    eligible_ids = {
        str(task.get("id") or "").strip()
        for task in eligible_tasks
        if str(task.get("id") or "").strip()
    }
    observations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for task in unclaimed_tasks:
        task_id = str(task.get("id") or "").strip()
        if not task_id or task_id in seen_ids:
            continue
        seen_ids.add(task_id)
        observations.append(
            {
                "task_id": task_id,
                "tag_count": task_tag_count(task),
                "task_type": resolved_task_type_label(task, tag_count_rules),
                "batch_name": str(task.get("task_batch_name") or task.get("batch_name") or ""),
                "eligible": task_id in eligible_ids,
                "previously_claimed": (
                    claim_history_by_task_id.get(task_id)
                    if claim_history_by_task_id is not None
                    else None
                ),
            }
        )
    return observations


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


def task_stage_url(task_id: str) -> str:
    return f"{task_url(task_id)}/stage/task"


def stagecraft_search_request_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tags_search_type = str(payload.get("tags_search_type") or "ALL").upper()
    if tags_search_type not in {"ALL", "ANY"}:
        tags_search_type = "ALL"
    variables: dict[str, Any] = {
        "filter": payload.get("filter") or "CLAIMABLE",
        "pageSize": int(payload.get("page_size") or 20),
        "campaignId": payload.get("campaign_id"),
        "tagsSearchType": tags_search_type,
        "includeStageOptions": bool(payload.get("include_stage_options", False)),
    }
    optional_variables = {
        "taskBatchId": payload.get("task_batch_id"),
        "taskBatchStatus": payload.get("task_batch_status"),
        "cursor": payload.get("cursor"),
        "query": payload.get("query"),
        "stageKeys": payload.get("stage_keys"),
        "tags": payload.get("tags"),
        "excludeTags": payload.get("exclude_tags"),
    }
    variables.update(
        (key, value)
        for key, value in optional_variables.items()
        if value not in (None, "", [])
    )
    return [
        {
            "operationName": "StagecraftSearch",
            "variables": variables,
            "query": STAGECRAFT_SEARCH_QUERY,
        }
    ]


def normalize_stagecraft_search_response(body: Any, payload: dict[str, Any]) -> dict[str, Any]:
    first = body[0] if isinstance(body, list) and body else body
    if not isinstance(first, dict):
        raise RuntimeError("StagecraftSearch returned an invalid GraphQL response.")
    if first.get("errors"):
        preview = json.dumps(first["errors"], ensure_ascii=False, separators=(",", ":"))[:1200]
        raise RuntimeError(f"StagecraftSearch GraphQL error: {preview}")
    data = first.get("data")
    search = data.get("stagecraftSearch") if isinstance(data, dict) else None
    if not isinstance(search, dict):
        raise RuntimeError("StagecraftSearch response did not contain stagecraftSearch data.")

    raw_results = search.get("results")
    results = raw_results if isinstance(raw_results, list) else []
    tasks: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        raw_task = result.get("task")
        if not isinstance(raw_task, dict) or not raw_task.get("id"):
            continue
        stage = result.get("stage") if isinstance(result.get("stage"), dict) else {}
        owner = (
            result.get("mostRecentlyDecidedStageOwner")
            if isinstance(result.get("mostRecentlyDecidedStageOwner"), dict)
            else {}
        )
        title = raw_task.get("title") or "Temporary empty task title"
        tasks.append(
            {
                "id": raw_task.get("id"),
                "campaign_id": payload.get("campaign_id"),
                "task_batch_id": payload.get("task_batch_id"),
                "task_batch_name": raw_task.get("taskBatchName"),
                "title": title,
                "description": title,
                "tags": raw_task.get("tags") if isinstance(raw_task.get("tags"), list) else [],
                "kind": "widget-layout",
                "workflow_status": "unclaimed",
                "stagecraft_task_status": raw_task.get("stagecraftTaskStatus"),
                "status_updated_at": raw_task.get("statusUpdatedAt"),
                "is_template_task": raw_task.get("isTemplateTask"),
                "stage_key": stage.get("key"),
                "stage_label": stage.get("label"),
                "previous_stage_labels": result.get("previousStageLabels"),
                "most_recently_decided_stage_label": result.get("mostRecentlyDecidedStageLabel"),
                "most_recently_decided_stage_owner_id": owner.get("id"),
                "most_recently_decided_stage_owner_email": owner.get("email"),
                "claimed_at": result.get("claimedAt"),
                "completed_at": result.get("completedAt"),
            }
        )

    next_cursor = search.get("nextCursor")
    pagination: dict[str, Any] = {
        "page": int(payload.get("_cursor_page") or 0),
        "page_size": int(payload.get("page_size") or 20),
        "cursor": payload.get("cursor"),
        "next_cursor": next_cursor,
        "search_api": "stagecraft",
    }
    if not payload.get("cursor") and not next_cursor:
        pagination["count"] = len(tasks)
    return {"tasks": tasks, "pagination": pagination}


def legacy_task_detail_payload(campaign_id: str, task_id: str) -> dict[str, Any]:
    payload = default_search_payload(campaign_id, 20)
    payload["workflow_statuses"] = [
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
    payload["exclude_declined"] = False
    payload["include_tags"] = True
    payload["query"] = task_id
    return payload


def enrich_stagecraft_search_tasks(
    data: dict[str, Any],
    headers: dict[str, str],
    campaign_id: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Restore batch metadata omitted by campaign-wide StagecraftSearch results."""
    raw_tasks = data.get("tasks") if isinstance(data, dict) else None
    tasks = raw_tasks if isinstance(raw_tasks, list) else []
    enriched_tasks: list[dict[str, Any]] = []
    missing_task_ids: list[str] = []

    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("task_batch_name"):
            enriched_tasks.append(task)
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        response = request_with_retries(
            "POST",
            SEARCH_URL,
            headers=headers,
            data=json.dumps(legacy_task_detail_payload(campaign_id, task_id), separators=(",", ":")),
            session=session,
        )
        if response.status_code in (401, 403):
            raise RuntimeError(f"auth failed during task metadata lookup: HTTP {response.status_code}")
        response.raise_for_status()
        body = response.json()
        details = body.get("tasks") if isinstance(body, dict) else None
        detail = next(
            (
                candidate
                for candidate in (details if isinstance(details, list) else [])
                if isinstance(candidate, dict) and str(candidate.get("id") or "") == task_id
            ),
            None,
        )
        if not isinstance(detail, dict) or not (
            detail.get("task_batch_name") or detail.get("batch_name")
        ):
            missing_task_ids.append(task_id)
            continue
        enriched = dict(task)
        for key in ("task_batch_id", "task_batch_name", "batch_name", "kind"):
            if detail.get(key) is not None:
                enriched[key] = detail[key]
        enriched_tasks.append(enriched)

    result = dict(data)
    result["tasks"] = enriched_tasks
    if missing_task_ids:
        result["metadata_lookup_missing_task_ids"] = missing_task_ids
    return result


def poll_once(
    headers: dict[str, str],
    payload: dict[str, Any],
    session: requests.Session | None = None,
) -> dict[str, Any]:
    stagecraft = is_stagecraft_search_payload(payload)
    response = request_with_retries(
        "POST",
        GRAPHQL_URL if stagecraft else SEARCH_URL,
        headers=headers,
        data=json.dumps(
            stagecraft_search_request_payload(payload) if stagecraft else payload,
            separators=(",", ":"),
        ),
        session=session,
    )
    if response.status_code in (401, 403):
        raise RuntimeError(f"auth failed: HTTP {response.status_code} {response.text[:160]}")
    response.raise_for_status()
    body = response.json()
    if not stagecraft:
        return body
    normalized = normalize_stagecraft_search_response(body, payload)
    return enrich_stagecraft_search_tasks(
        normalized,
        headers,
        str(payload.get("campaign_id") or ""),
        session=session,
    )


def task_page_signature(tasks: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        str(task.get("id") or json.dumps(task, sort_keys=True, default=str))
        for task in tasks
    )


def poll_all_cursor_pages(
    headers: dict[str, str],
    payload: dict[str, Any],
    max_pages: int,
    first_page: dict[str, Any] | None,
    stop_requested: Callable[[], bool] | None,
    session: requests.Session | None,
) -> list[dict[str, Any]]:
    cursor_payload = dict(payload)
    cursor_payload["_cursor_page"] = 0
    pages: list[dict[str, Any]] = []
    seen_page_signatures: set[tuple[str, ...]] = set()
    seen_cursors: set[str] = set()
    pending_first_page = first_page

    for page_index in range(max_pages):
        if pages and stop_requested is not None and stop_requested():
            break
        if pending_first_page is not None:
            data = pending_first_page
            pending_first_page = None
        elif session is None:
            data = poll_once(headers, cursor_payload)
        else:
            data = poll_once(headers, cursor_payload, session=session)

        raw_tasks = data.get("tasks", []) if isinstance(data, dict) else []
        tasks = raw_tasks if isinstance(raw_tasks, list) else []
        signature = task_page_signature(tasks)
        if signature and signature in seen_page_signatures:
            break
        if signature:
            seen_page_signatures.add(signature)
        pages.append(data)

        pagination = data.get("pagination") if isinstance(data, dict) else None
        pagination = pagination if isinstance(pagination, dict) else {}
        next_cursor = str(pagination.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor_payload = dict(cursor_payload)
        cursor_payload["cursor"] = next_cursor
        cursor_payload["_cursor_page"] = page_index + 1

    return pages


def poll_all_pages(
    headers: dict[str, str],
    payload: dict[str, Any],
    max_pages: int = MAX_SEARCH_PAGES,
    first_page: dict[str, Any] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Fetch every task-search page, stopping safely on an empty or repeated page."""
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")

    if is_stagecraft_search_payload(payload):
        return poll_all_cursor_pages(
            headers,
            payload,
            max_pages,
            first_page,
            stop_requested,
            session,
        )

    page_payload = dict(payload)
    try:
        page_number = int(page_payload.get("page", 0))
    except (TypeError, ValueError):
        page_number = 0
    page_payload["page"] = page_number

    pages: list[dict[str, Any]] = []
    seen_page_signatures: set[tuple[str, ...]] = set()

    pending_first_page = first_page
    for _ in range(max_pages):
        if pages and stop_requested is not None and stop_requested():
            break
        if pending_first_page is not None:
            data = pending_first_page
            pending_first_page = None
        elif session is None:
            data = poll_once(headers, page_payload)
        else:
            data = poll_once(headers, page_payload, session=session)
        raw_tasks = data.get("tasks", []) if isinstance(data, dict) else []
        tasks = raw_tasks if isinstance(raw_tasks, list) else []
        signature = task_page_signature(tasks)

        # A backend that ignores the page parameter must not create an infinite loop
        # or duplicate the same tasks in the current poll.
        if signature and signature in seen_page_signatures:
            break
        if signature:
            seen_page_signatures.add(signature)
        pages.append(data)

        if not tasks:
            break

        pagination = data.get("pagination") if isinstance(data, dict) else None
        pagination = pagination if isinstance(pagination, dict) else {}
        try:
            page_size = int(pagination.get("page_size", page_payload.get("page_size", len(tasks))))
        except (TypeError, ValueError):
            page_size = len(tasks)
        if page_size < 1 or len(tasks) < page_size:
            break

        try:
            response_page = int(pagination.get("page", page_payload["page"]))
        except (TypeError, ValueError):
            response_page = int(page_payload["page"])
        try:
            total_count = int(pagination["count"])
        except (KeyError, TypeError, ValueError):
            total_count = None
        if total_count is not None and total_count >= 0 and (response_page + 1) * page_size >= total_count:
            break
        page_payload = dict(page_payload)
        page_payload["page"] = response_page + 1
        if "random_seed" in pagination:
            page_payload["random_seed"] = pagination["random_seed"]

    return pages


def campaign_search_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an unscoped campaign search used to choose the cheapest complete scan."""
    campaign_payload = dict(payload)
    campaign_payload.pop("task_batch_id", None)
    campaign_payload.pop("random_seed", None)
    if is_stagecraft_search_payload(campaign_payload):
        campaign_payload["cursor"] = None
        campaign_payload["_cursor_page"] = 0
    else:
        campaign_payload["page"] = 0
    return campaign_payload


def search_page_count(data: dict[str, Any], payload: dict[str, Any]) -> int | None:
    """Return the server-reported page count, or None when it cannot be trusted."""
    count = search_total_count(data)
    pagination = data.get("pagination") if isinstance(data, dict) else None
    pagination = pagination if isinstance(pagination, dict) else {}
    try:
        page_size = int(pagination.get("page_size", payload.get("page_size")))
    except (TypeError, ValueError):
        return None
    if count is None or page_size < 1:
        return None
    return (count + page_size - 1) // page_size


def search_total_count(data: dict[str, Any]) -> int | None:
    """Return a non-negative server-reported task count when available."""
    pagination = data.get("pagination") if isinstance(data, dict) else None
    pagination = pagination if isinstance(pagination, dict) else {}
    try:
        count = int(pagination["count"])
    except (KeyError, TypeError, ValueError):
        return None
    return count if count >= 0 else None


def claim_payload(task_id: str) -> list[dict[str, Any]]:
    return [
        {
            "operationName": "StagecraftClaimStage",
            "variables": {
                "taskId": task_id,
                "stageKey": "task",
            },
            "query": STAGECRAFT_CLAIM_STAGE_QUERY,
        }
    ]


def task_history_payload(task_id: str) -> list[dict[str, Any]]:
    return [
        {
            "operationName": "TaskHistory",
            "variables": {"taskId": task_id},
            "query": TASK_HISTORY_QUERY,
        }
    ]


def _task_history_from_graphql_item(item: Any) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        raise RuntimeError("TaskHistory returned an invalid GraphQL response.")
    if item.get("errors"):
        preview = json.dumps(item["errors"], ensure_ascii=False, separators=(",", ":"))[:1200]
        raise RuntimeError(f"TaskHistory GraphQL error: {preview}")

    data = item.get("data")
    history = data.get("taskHistory") if isinstance(data, dict) else None
    if history is None:
        raise RuntimeError("TaskHistory response did not include data.taskHistory.")
    if isinstance(history, dict):
        history = [history]
    if not isinstance(history, list) or not all(isinstance(version, dict) for version in history):
        raise RuntimeError("TaskHistory returned an invalid history list.")
    return history


def fetch_task_histories(
    headers: dict[str, str],
    task_ids: list[str],
    session: requests.Session | None = None,
) -> dict[str, list[dict[str, Any]]]:
    unique_task_ids = list(dict.fromkeys(str(task_id).strip() for task_id in task_ids if str(task_id).strip()))
    if not unique_task_ids:
        return {}

    payload = [task_history_payload(task_id)[0] for task_id in unique_task_ids]
    response = request_with_retries(
        "POST",
        f"{BASE_URL}/api/graphql",
        headers=headers,
        data=json.dumps(payload, separators=(",", ":")),
        session=session,
    )
    if response.status_code in (401, 403):
        raise RuntimeError(
            f"auth failed during task history check: HTTP {response.status_code} {response.text[:160]}"
        )
    response.raise_for_status()

    body = response.json()
    items = body if isinstance(body, list) else [body]
    if len(items) != len(unique_task_ids):
        raise RuntimeError(
            f"TaskHistory returned {len(items)} result(s) for {len(unique_task_ids)} task(s)."
        )
    return {
        task_id: _task_history_from_graphql_item(item)
        for task_id, item in zip(unique_task_ids, items)
    }


def fetch_task_history(
    headers: dict[str, str],
    task_id: str,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    return fetch_task_histories(headers, [task_id], session=session)[task_id]


def was_previously_claimed(history: list[dict[str, Any]]) -> bool:
    for history_version in history:
        entries = history_version.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("workflowStatus") or "").upper()
            if status in {"IN_PROGRESS", "COMPLETED"}:
                return True
    return False


def completed_history_evidence(
    history: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the first prior completion, regardless of the Feather user identity."""
    for history_version in history:
        entries = history_version.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("workflowStatus") or "").upper() != "COMPLETED":
                continue

            actor = entry.get("user")
            if not isinstance(actor, dict) or not actor:
                task_version = entry.get("taskVersion")
                actor = task_version.get("user") if isinstance(task_version, dict) else None
            actor = actor if isinstance(actor, dict) else {}

            return {
                "history_version": history_version.get("version"),
                "completed_at": entry.get("createdAt"),
                "completed_by_user_id": actor.get("id"),
                "completed_by_user_email": actor.get("email"),
                "completed_by_unknown_user": not bool(actor.get("id") or actor.get("email")),
            }
    return None


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


def credential_account_summary(user: dict[str, Any]) -> dict[str, str | None]:
    user_id = str(user.get("id") or "").strip()
    email = str(user.get("email") or "").strip()
    display_name = str(
        user.get("name")
        or user.get("display_name")
        or user.get("displayName")
        or user.get("full_name")
        or user.get("fullName")
        or ""
    ).strip()
    return {
        "label": email or display_name or user_id or "Unknown Feather account",
        "email": email or None,
        "display_name": display_name or None,
        "user_id": user_id or None,
    }


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
    for data in poll_all_pages(headers, verify_payload, session=session):
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
    for data in poll_all_pages(headers, payload, session=session):
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


def start_insightful_after_claim(config: MonitorConfig, emit: Emit = print) -> dict[str, Any]:
    if not config.start_insightful:
        return {"state": "disabled"}
    emit(
        f'INSIGHTFUL_STARTING task_prefix="{config.insightful_task_prefix}"',
        flush=True,
    )
    try:
        result = start_insightful_timer(config.insightful_task_prefix)
        emit(
            f'INSIGHTFUL_{result["state"].upper()} task={result.get("task_id", "unknown")}',
            flush=True,
        )
        return result
    except Exception as exc:
        error = str(exc).strip() or type(exc).__name__
        emit(f"INSIGHTFUL_FAILED {error}", flush=True)
        return {"state": "failed", "error": error}


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
        update = data.get("stagecraftClaimStage") if isinstance(data, dict) else None
        definitive_result = bool(isinstance(first, dict) and (isinstance(update, dict) or first.get("errors")))
        claim_succeeded = (
            response.status_code == 200
            and isinstance(update, dict)
            and update.get("id") == task_id
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    parser.add_argument(
        "--start-insightful",
        action="store_true",
        help="After a successful claim, open Insightful and start the configured task timer.",
    )
    parser.add_argument(
        "--insightful-task-prefix",
        default=os.environ.get("INSIGHTFUL_TASK_PREFIX", DEFAULT_TASK_PREFIX),
        help=f'Insightful task-name prefix to start (default: "{DEFAULT_TASK_PREFIX}").',
    )
    parser.add_argument("--save", default="last_found_task.json", help="Where to save the found task JSON. Use empty string to disable.")
    parser.add_argument("--log-file", help="Append stdout/stderr to this file.")
    parser.add_argument("--status-file", help="Continuously write current monitor status as JSON.")
    parser.add_argument("--task-kind", help="Optional x-feather-client-task-kind header for claim.")
    parser.add_argument("--coordination-url", default=os.environ.get("FEATHER_COORDINATION_URL"), help="Shared account coordination server URL.")
    parser.add_argument("--coordination-token", default=os.environ.get("FEATHER_COORDINATION_TOKEN"), help="Coordination server bearer token.")
    parser.add_argument("--coordination-owner", default=os.environ.get("FEATHER_COORDINATION_OWNER"), help="Human-readable operator name shown to other users.")
    parser.add_argument("--coordination-state-file", default=os.environ.get("FEATHER_COORDINATION_STATE_FILE", "outputs/coordination_lease.json"), help="Local file used to release a claimed-task lease.")
    parser.add_argument("--coordination-search-ttl", type=int, default=int(os.environ.get("FEATHER_COORDINATION_SEARCH_TTL", "120")))
    parser.add_argument("--coordination-working-ttl", type=int, default=int(os.environ.get("FEATHER_COORDINATION_WORKING_TTL", str(12 * 60 * 60))))
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


def _run_monitor_impl(
    config: MonitorConfig,
    stop_event: Any | None = None,
    emit: Emit = print,
    status_callback: StatusCallback | None = None,
    session: requests.Session | None = None,
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
    try:
        tag_count_rules = normalize_tag_count_rules(config.tag_count_rules)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if config.start_insightful and not config.insightful_task_prefix.strip():
        raise SystemExit("Use a non-empty --insightful-task-prefix when --start-insightful is enabled.")
    tag_count_filter = tag_count_filter_payload(config.tag_count_min, config.tag_count_max)
    batch_regex = effective_batch_regex(config.batch_regex, config.batch_suffix)
    compile_batch_regex(batch_regex)

    coordination_lease: CoordinationLease | None = None
    credential_account: dict[str, str | None] | None = None

    def stop_requested() -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    def update_status(**status: Any) -> None:
        status.setdefault("batch_regex", batch_regex)
        status.setdefault("tag_count_filter", tag_count_filter)
        status.setdefault("tag_count_rules", tag_count_rules)
        status.setdefault("coordination_enabled", bool(config.coordination_url))
        status.setdefault("credential_account", credential_account)
        if coordination_lease is not None:
            status.setdefault("coordination_account_key", coordination_lease.account_key)
            status.setdefault("coordination_owner", coordination_lease.config.owner_label)
            status.setdefault("coordination_phase", coordination_lease.phase)
        payload = status_payload(**status)
        write_status_payload(
            config.status_file,
            {key: value for key, value in payload.items() if key != "task_observations"},
        )
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
    client_git_hash = header_value(curl_text, "x-feather-client-git-hash") or CLIENT_GIT_HASH
    user_agent = header_value(curl_text, "user-agent")
    payload["include_tags"] = True
    campaign_url = f"{BASE_URL}/campaigns/{config.campaign_id}?tab=tasks&tasks-tab=unclaimed"
    search_headers = build_headers(
        cookie,
        config.campaign_id,
        campaign_url,
        client_git_hash=client_git_hash,
        user_agent=user_agent,
    )
    user = current_user(search_headers, session=session)
    credential_account = credential_account_summary(user)
    update_status(
        state="starting",
        phase="credential_verified",
        campaign_id=config.campaign_id,
        claim=config.claim,
        tag_count_filter=tag_count_filter,
    )
    if config.claim and config.coordination_url:
        if not config.coordination_token:
            message = "FEATHER_COORDINATION_URL is set but FEATHER_COORDINATION_TOKEN is missing."
            emit(f"COORDINATION_ERROR {message}", flush=True)
            update_status(
                state="coordination_error",
                campaign_id=config.campaign_id,
                claim=config.claim,
                coordination_error=message,
            )
            return 2
        coordination_config = CoordinationConfig(
            url=config.coordination_url,
            service_token=config.coordination_token,
            owner_label=config.coordination_owner or default_owner_label(),
            state_file=config.coordination_state_file,
            search_ttl_seconds=config.coordination_search_ttl,
            working_ttl_seconds=config.coordination_working_ttl,
        )
        coordination_lease = CoordinationLease(
            coordination_config,
            account_key_for_user(user),
            config.campaign_id,
        )
        config._coordination_lease = coordination_lease
        try:
            coordination_lease.acquire()
        except LeaseUnavailable as exc:
            holder = exc.lease
            owner = holder.get("owner_label") or "another operator"
            phase = holder.get("phase") or "busy"
            reason = f"Shared Feather account is already {phase} under {owner}."
            emit(f"CLAIM_BLOCKED coordination owner={owner} phase={phase}", flush=True)
            update_status(
                state="blocked_coordination",
                campaign_id=config.campaign_id,
                claim=config.claim,
                blocking_reason=reason,
                coordination_holder=holder,
                coordination_owner=owner,
                coordination_phase=phase,
            )
            return 0
        except CoordinationError as exc:
            message = str(exc)
            emit(f"COORDINATION_ERROR {message}", flush=True)
            update_status(
                state="coordination_error",
                campaign_id=config.campaign_id,
                claim=config.claim,
                coordination_error=message,
            )
            return 2
        emit(
            f"COORDINATION_ACQUIRED account={coordination_lease.account_key} "
            f"owner={coordination_config.owner_label}",
            flush=True,
        )
    if config.claim:
        in_progress_task = find_current_in_progress_task(
            search_headers,
            config.campaign_id,
            config.page_size,
            user,
            session=session,
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
        session=session,
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
    claim_history_cache: dict[str, list[dict[str, Any]]] = {}
    last_batch_signature = tuple(current_batch_search_ids)
    last_distribution_full_scan_at: float | None = None
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
        poll_observation_id = f"{time.time_ns()}-{os.getpid()}"
        poll_errors: list[tuple[str, requests.exceptions.RequestException]] = []
        if config.batch_name or batch_regex:
            try:
                allowed_batch_refs, search_payloads = resolve_batch_searches(
                    search_headers,
                    config.campaign_id,
                    payload,
                    config.batch_id,
                    config.batch_name,
                    batch_regex,
                    session=session,
                )
                current_batch_search_ids = batch_search_id_list(search_payloads)
                batch_signature = tuple(current_batch_search_ids)
                if batch_signature != last_batch_signature:
                    emit_batch_targets(allowed_batch_refs, search_payloads)
                    last_batch_signature = batch_signature
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if config.once:
                    raise
                poll_errors.append(("batch_refs", exc))
                emit(
                    f"[{now}] {recoverable_poll_error_name(exc)} target=batch_refs "
                    f"attempts={SAFE_REQUEST_RETRIES + 1} timeout={REQUEST_TIMEOUT_SECONDS}s "
                    f"using_previous_batch_filters={len(current_batch_search_ids)} error={exc}",
                    flush=True,
                )

        tasks: list[dict[str, Any]] = []
        distribution_tasks: list[dict[str, Any]] = []
        response_by_task_id: dict[str, dict[str, Any]] = {}
        seen_poll_task_ids: set[str] = set()
        seen_distribution_task_ids: set[str] = set()
        total_unclaimed_count: int | None = None
        page_searches = 0
        search_mode = "batch"
        searches_to_run = search_payloads
        campaign_pages: list[dict[str, Any]] | None = None
        distribution_campaign_pages: list[dict[str, Any]] | None = None
        probe_data: dict[str, Any] | None = None

        if config.batch_id or config.batch_name or batch_regex:
            probe_payload = campaign_search_payload(payload)
            try:
                probe_data = poll_once(search_headers, probe_payload, session=session)
                page_searches += 1
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if config.once:
                    raise
                poll_errors.append(("campaign_probe", exc))
                emit(
                    f"[{now}] {recoverable_poll_error_name(exc)} target=tasks "
                    f"search_mode=campaign_probe attempts={SAFE_REQUEST_RETRIES + 1} "
                    f"timeout={REQUEST_TIMEOUT_SECONDS}s error={exc}",
                    flush=True,
                )
            else:
                total_unclaimed_count = search_total_count(probe_data)
                campaign_page_total = search_page_count(probe_data, probe_payload)
                if is_stagecraft_search_payload(probe_payload) or (
                    campaign_page_total is not None
                    and campaign_page_total <= len(search_payloads)
                ):
                    try:
                        if stop_event is None:
                            campaign_pages = poll_all_pages(
                                search_headers,
                                probe_payload,
                                first_page=probe_data,
                                session=session,
                            )
                        else:
                            campaign_pages = poll_all_pages(
                                search_headers,
                                probe_payload,
                                first_page=probe_data,
                                stop_requested=stop_requested,
                                session=session,
                            )
                    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                        if config.once:
                            raise
                        poll_errors.append(("campaign_pages", exc))
                        campaign_pages = None
                        search_mode = "batch_fallback"
                        emit(
                            f"[{now}] {recoverable_poll_error_name(exc)} target=tasks "
                            f"search_mode=campaign attempts={SAFE_REQUEST_RETRIES + 1} "
                            f"timeout={REQUEST_TIMEOUT_SECONDS}s falling_back_to=batch error={exc}",
                            flush=True,
                        )
                    else:
                        search_mode = "campaign"
                        page_searches += max(0, len(campaign_pages) - 1)
                        if total_unclaimed_count is None:
                            total_unclaimed_count = len(
                                {
                                    str(task.get("id") or "").strip()
                                    for page in campaign_pages
                                    for task in page.get("tasks", [])
                                    if str(task.get("id") or "").strip()
                                }
                            )
                        searches_to_run = []
                        last_distribution_full_scan_at = time.monotonic()
                else:
                    page_label = "unknown" if campaign_page_total is None else str(campaign_page_total)
                    emit(
                        f"[{now}] search_mode=batch campaign_pages={page_label} "
                        f"matched_batches={len(search_payloads)}",
                        flush=True,
                    )

        distribution_scan_due = (
            campaign_pages is None
            and probe_data is not None
            and (
                last_distribution_full_scan_at is None
                or time.monotonic() - last_distribution_full_scan_at
                >= DISTRIBUTION_FULL_SCAN_INTERVAL_SECONDS
            )
        )
        if distribution_scan_due:
            try:
                if stop_event is None:
                    distribution_campaign_pages = poll_all_pages(
                        search_headers,
                        probe_payload,
                        first_page=probe_data,
                        session=session,
                    )
                else:
                    distribution_campaign_pages = poll_all_pages(
                        search_headers,
                        probe_payload,
                        first_page=probe_data,
                        stop_requested=stop_requested,
                        session=session,
                    )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if config.once:
                    raise
                poll_errors.append(("distribution_campaign_pages", exc))
                emit(
                    f"[{now}] {recoverable_poll_error_name(exc)} target=distribution "
                    f"search_mode=campaign attempts={SAFE_REQUEST_RETRIES + 1} "
                    f"timeout={REQUEST_TIMEOUT_SECONDS}s error={exc}",
                    flush=True,
                )
            else:
                last_distribution_full_scan_at = time.monotonic()
                page_searches += max(0, len(distribution_campaign_pages) - 1)
                campaign_pages = distribution_campaign_pages
                search_mode = "campaign"
                searches_to_run = []
                emit(
                    f"[{now}] distribution_scan=campaign "
                    f"pages={len(distribution_campaign_pages)} interval_seconds="
                    f"{DISTRIBUTION_FULL_SCAN_INTERVAL_SECONDS:g}",
                    flush=True,
                )

        page_groups: list[tuple[str | None, list[dict[str, Any]]]] = []
        if campaign_pages is not None:
            page_groups.append((None, campaign_pages))

        for batch_id, search_payload in searches_to_run:
            try:
                if stop_event is None:
                    page_responses = poll_all_pages(
                        search_headers,
                        search_payload,
                        session=session,
                    )
                else:
                    page_responses = poll_all_pages(
                        search_headers,
                        search_payload,
                        stop_requested=stop_requested,
                        session=session,
                    )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if config.once:
                    raise
                poll_errors.append((batch_id or "all_batches", exc))
                emit(
                    f"[{now}] {recoverable_poll_error_name(exc)} target=tasks "
                    f"batch_id={batch_id or '*'} attempts={SAFE_REQUEST_RETRIES + 1} "
                    f"timeout={REQUEST_TIMEOUT_SECONDS}s error={exc}",
                    flush=True,
                )
                continue
            page_searches += len(page_responses)
            page_groups.append((batch_id, page_responses))

        for _batch_id, page_responses in page_groups:
            for data in page_responses:
                for task in data.get("tasks", []):
                    distribution_task_id = str(task.get("id") or "").strip()
                    distribution_key = distribution_task_id or json.dumps(task, sort_keys=True, default=str)
                    if distribution_key not in seen_distribution_task_ids:
                        seen_distribution_task_ids.add(distribution_key)
                        distribution_tasks.append(task)
                    if search_mode == "campaign" and not task_matches_filters(
                        task,
                        config.batch_id,
                        config.batch_name,
                        batch_regex,
                        allowed_batch_refs,
                        None,
                        None,
                    ):
                        continue
                    task_id = str(task.get("id") or "").strip()
                    if task_id:
                        if task_id in seen_poll_task_ids:
                            continue
                        seen_poll_task_ids.add(task_id)
                        response_by_task_id[task_id] = data
                    tasks.append(task)

        if distribution_campaign_pages is not None:
            distribution_tasks = []
            seen_distribution_task_ids.clear()
            for data in distribution_campaign_pages:
                for task in data.get("tasks", []):
                    distribution_task_id = str(task.get("id") or "").strip()
                    distribution_key = distribution_task_id or json.dumps(
                        task,
                        sort_keys=True,
                        default=str,
                    )
                    if distribution_key in seen_distribution_task_ids:
                        continue
                    seen_distribution_task_ids.add(distribution_key)
                    distribution_tasks.append(task)
        if not (config.batch_id or config.batch_name or batch_regex):
            total_unclaimed_count = len(tasks)

        matching_tasks = [
            task
            for task in tasks
            if task_matches_filters(
                task,
                config.batch_id,
                config.batch_name,
                batch_regex,
                allowed_batch_refs,
                config.tag_count_min,
                config.tag_count_max,
                tag_count_rules,
            )
        ]
        matching_count = len(matching_tasks)
        distribution_eligible_tasks = [
            task
            for task in distribution_tasks
            if task_matches_filters(
                task,
                config.batch_id,
                config.batch_name,
                batch_regex,
                allowed_batch_refs,
                config.tag_count_min,
                config.tag_count_max,
                tag_count_rules,
            )
        ]
        current_task_ids = {
            str(task.get("id") or "").strip()
            for task in tasks
            if str(task.get("id") or "").strip()
        }
        for cached_task_id in list(claim_history_cache):
            if cached_task_id not in current_task_ids:
                claim_history_cache.pop(cached_task_id, None)

        eligible_task_ids = (
            list(
                dict.fromkeys(
                    str(task.get("id") or "").strip()
                    for task in matching_tasks
                    if str(task.get("id") or "").strip()
                )
            )
            if config.claim
            else []
        )
        unchecked_task_ids = [
            task_id for task_id in eligible_task_ids if task_id not in claim_history_cache
        ]
        claim_history_errors: list[str] = []
        for start in range(0, len(unchecked_task_ids), TASK_HISTORY_BATCH_SIZE):
            batch_task_ids = unchecked_task_ids[start:start + TASK_HISTORY_BATCH_SIZE]
            try:
                histories = fetch_task_histories(
                    search_headers,
                    batch_task_ids,
                    session=session,
                )
            except (requests.exceptions.RequestException, RuntimeError, ValueError) as exc:
                claim_history_errors.append(str(exc))
                emit(
                    f"TASK_HISTORY_BATCH_FAILED tasks={len(batch_task_ids)} "
                    f"error={type(exc).__name__} retry=next_poll",
                    flush=True,
                )
                continue
            for history_task_id, history in histories.items():
                claim_history_cache[history_task_id] = history

        previously_claimed_count = (
            sum(
                1
                for task_id in eligible_task_ids
                if task_id in claim_history_cache
                and was_previously_claimed(claim_history_cache[task_id])
            )
            if config.claim
            else None
        )
        claim_history_sample_complete = (
            len(eligible_task_ids) == matching_count
            and all(task_id in claim_history_cache for task_id in eligible_task_ids)
            if config.claim
            else None
        )
        history_sample_complete = not poll_errors and total_unclaimed_count is not None
        distribution_is_campaign = (
            distribution_campaign_pages is not None
            or search_mode == "campaign"
            or not (
                config.batch_id
                or config.batch_name
                or batch_regex
                or batch_id_from_payload(payload)
            )
        )
        distributions = task_distributions(
            distribution_tasks,
            distribution_eligible_tasks,
            total_unclaimed_count=total_unclaimed_count,
            complete=(
                not poll_errors
                and distribution_is_campaign
                and total_unclaimed_count is not None
                and len(distribution_tasks) == total_unclaimed_count
            ),
            source="campaign" if distribution_is_campaign else "matched_batches",
            tag_count_rules=tag_count_rules,
        )
        claim_history_by_task_id = (
            {
                task_id: was_previously_claimed(claim_history_cache[task_id])
                for task_id in eligible_task_ids
                if task_id in claim_history_cache
            }
            if config.claim and claim_history_sample_complete is True
            else None
        )
        observation_records = task_observation_records(
            distribution_tasks,
            distribution_eligible_tasks,
            tag_count_rules,
            claim_history_by_task_id,
        )
        poll_error_fields: dict[str, Any] = {}
        if poll_errors:
            poll_error_fields = {
                "poll_error_count": len(poll_errors),
                "last_error": str(poll_errors[-1][1])[:500],
            }
        if claim_history_errors:
            poll_error_fields["claim_history_error_count"] = len(claim_history_errors)
            poll_error_fields["claim_history_last_error"] = claim_history_errors[-1][:500]

        poll_observation_emitted = False

        def emit_poll_observation() -> None:
            nonlocal poll_observation_emitted
            if poll_observation_emitted:
                return
            poll_observation_emitted = True
            emit(
                f"[{now}] tasks={len(tasks)} batch_searches={len(searches_to_run)} "
                f"page_searches={page_searches} search_mode={search_mode} "
                f"total_unclaimed={total_unclaimed_count if total_unclaimed_count is not None else '?'} "
                f"matching={matching_count} previously_claimed={previously_claimed_count} "
                f"claim_history_complete={'true' if claim_history_sample_complete else 'false'}",
                flush=True,
            )
            if tasks:
                emit_task_tag_counts(tasks, emit)
            update_status(
                state="monitoring",
                phase="polling_with_errors" if poll_errors else "polling",
                campaign_id=config.campaign_id,
                claim=config.claim,
                batch_regex=batch_regex,
                tag_count_filter=tag_count_filter,
                active_batch_matches=len(allowed_batch_refs),
                server_batch_filters=current_batch_search_ids,
                unclaimed_count=len(tasks),
                total_unclaimed_count=total_unclaimed_count,
                matching_count=matching_count,
                task_distributions=distributions,
                task_observations=observation_records,
                poll_observation_id=poll_observation_id,
                previously_claimed_count=previously_claimed_count,
                claim_history_checked_count=sum(
                    1 for task_id in eligible_task_ids if task_id in claim_history_cache
                ),
                claim_history_sample_complete=claim_history_sample_complete,
                history_sample_complete=history_sample_complete,
                search_pages=page_searches,
                last_poll=now,
                **poll_error_fields,
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
                tag_count_rules,
            ):
                continue

            task_id = task.get("id")
            if not task_id or task_id in seen:
                continue

            seen.add(task_id)
            title = task_log_title(task)
            current_tag_count = task_tag_count(task)
            summary = batch_summary(task)
            save_path = config.save or None
            claim_succeeded = False
            if config.claim:
                in_progress_task = find_current_in_progress_task(
                    search_headers,
                    config.campaign_id,
                    config.page_size,
                    user,
                    session=session,
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
                if coordination_lease is not None:
                    try:
                        coordination_lease.ensure_owned()
                    except CoordinationError as exc:
                        message = str(exc)
                        emit(f"CLAIM_BLOCKED coordination_lost error={message}", flush=True)
                        update_status(
                            state="blocked_coordination",
                            campaign_id=config.campaign_id,
                            claim=config.claim,
                            task_id=task_id,
                            title=title,
                            blocking_reason="Shared-account coordination lease was lost before claim.",
                            coordination_error=message,
                        )
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
                        unclaimed_count=len(tasks),
                        last_poll=now,
                    )
                    return 0
                claim_headers = build_headers(
                    cookie,
                    config.campaign_id,
                    task_stage_url(task_id),
                    task_id=task_id,
                    task_kind=config.task_kind or task.get("kind"),
                    client_git_hash=client_git_hash,
                    user_agent=user_agent,
                )
                task_history = claim_history_cache.get(str(task_id))
                if task_history is None:
                    seen.discard(task_id)
                    emit(
                        f"TASK_HISTORY_CHECK_UNAVAILABLE_SKIP task={task_id} retry=next_poll",
                        flush=True,
                    )
                    continue

                completed_history = completed_history_evidence(task_history)
                if completed_history is not None:
                    actor = (
                        completed_history.get("completed_by_user_email")
                        or completed_history.get("completed_by_user_id")
                        or "unknown"
                    )
                    emit(
                        f"CLAIM_SKIPPED_PREVIOUSLY_COMPLETED task={task_id} "
                        f"version={completed_history.get('history_version')} actor={actor}",
                        flush=True,
                    )
                    continue

                emit(
                    f"TASK_HISTORY_SAFE task={task_id} versions={len(task_history)}",
                    flush=True,
                )
                claim_succeeded = print_claim_result(
                    claim_headers,
                    search_headers,
                    config.campaign_id,
                    config.page_size,
                    task_id,
                    emit=emit,
                    session=session,
                    user=user,
                )

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
            save_found(save_path, task, response_by_task_id.get(str(task_id), {"tasks": tasks}))
            if save_path:
                emit(f"SAVED {save_path}", flush=True)
            emit_poll_observation()

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
                    )
                    continue

                coordination_error = None
                if coordination_lease is not None:
                    try:
                        coordination_lease.mark_working(str(task_id))
                        emit(f"COORDINATION_WORKING task={task_id}", flush=True)
                    except CoordinationError as exc:
                        coordination_lease.phase = "working_unconfirmed"
                        coordination_error = str(exc)
                        emit(f"COORDINATION_WORKING_FAILED task={task_id} error={coordination_error}", flush=True)
                insightful_result = start_insightful_after_claim(config, emit=emit)
                update_status(
                    state="claimed",
                    campaign_id=config.campaign_id,
                    task_id=task_id,
                    title=title,
                    batch=summary,
                    tag_count=current_tag_count,
                    tag_count_filter=tag_count_filter,
                    saved=save_path,
                    coordination_error=coordination_error,
                    insightful=insightful_result,
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

            emit_poll_observation()
            emit("\a", end="", flush=True)
            if config.open_task:
                webbrowser.open(task_url(task_id))
            return 0

        emit_poll_observation()

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
            phase="sleeping_after_errors" if poll_errors else "sleeping",
            campaign_id=config.campaign_id,
            claim=config.claim,
            batch_regex=batch_regex,
            tag_count_filter=tag_count_filter,
            active_batch_matches=len(allowed_batch_refs),
            server_batch_filters=current_batch_search_ids,
            unclaimed_count=len(tasks),
            total_unclaimed_count=total_unclaimed_count,
            matching_count=matching_count,
            task_distributions=distributions,
            previously_claimed_count=previously_claimed_count,
            claim_history_checked_count=sum(
                1 for task_id in eligible_task_ids if task_id in claim_history_cache
            ),
            claim_history_sample_complete=claim_history_sample_complete,
            history_sample_complete=history_sample_complete,
            search_pages=page_searches,
            next_sleep_seconds=round(delay, 2),
            last_poll=now,
            **poll_error_fields,
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


def run_monitor(
    config: MonitorConfig,
    stop_event: Any | None = None,
    emit: Emit = print,
    status_callback: StatusCallback | None = None,
) -> int:
    emit(f"FEATHER_AUTO_VERSION {__version__}", flush=True)
    session = create_http_session()
    try:
        return _run_monitor_impl(
            config,
            stop_event=stop_event,
            emit=emit,
            status_callback=status_callback,
            session=session,
        )
    finally:
        try:
            session.close()
        finally:
            lease = config._coordination_lease
            config._coordination_lease = None
            if lease is not None:
                lease.close()


def recoverable_poll_error_name(exc: requests.exceptions.RequestException) -> str:
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
        start_insightful=args.start_insightful,
        insightful_task_prefix=args.insightful_task_prefix,
        coordination_url=args.coordination_url,
        coordination_token=args.coordination_token,
        coordination_owner=args.coordination_owner,
        coordination_state_file=args.coordination_state_file,
        coordination_search_ttl=args.coordination_search_ttl,
        coordination_working_ttl=args.coordination_working_ttl,
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
