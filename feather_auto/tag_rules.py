from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


def _optional_bound(value: Any, label: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer.") from exc
    if parsed < 0:
        raise ValueError(f"{label} must be >= 0.")
    return parsed


def normalize_tag_count_rules(value: Any) -> dict[str, dict[str, Any]]:
    """Validate task-type rules containing a batch-name mapping and tag bounds."""
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Task type tag rules must be an object.")

    normalized: dict[str, dict[str, Any]] = {}
    for raw_task_type, raw_bounds in value.items():
        task_type = str(raw_task_type or "").strip()
        if not task_type:
            raise ValueError("Task type tag rule names cannot be blank.")
        if raw_bounds in (None, ""):
            continue
        if not isinstance(raw_bounds, Mapping):
            raise ValueError(f"Tag rule for {task_type} must contain min/max bounds.")

        minimum = _optional_bound(raw_bounds.get("min"), f"{task_type} tag min")
        maximum = _optional_bound(raw_bounds.get("max"), f"{task_type} tag max")
        raw_batch_regex = (
            raw_bounds.get("batch_regex")
            if "batch_regex" in raw_bounds
            else raw_bounds.get("batchRegex")
        )
        batch_regex = str(raw_batch_regex or "").strip()
        if batch_regex:
            try:
                re.compile(batch_regex, re.I)
            except re.error as exc:
                raise ValueError(f"Invalid {task_type} batch regex: {exc}") from exc
        if minimum is None and maximum is None and not batch_regex:
            continue
        if minimum is not None and maximum is not None and maximum < minimum:
            raise ValueError(f"{task_type} tag max must be >= tag min.")

        bounds: dict[str, Any] = {}
        if batch_regex:
            bounds["batch_regex"] = batch_regex
        if minimum is not None:
            bounds["min"] = minimum
        if maximum is not None:
            bounds["max"] = maximum
        normalized[task_type] = bounds
    return normalized


def batch_mapping_enabled(rules: Mapping[str, Mapping[str, Any]] | None) -> bool:
    return any(str(rule.get("batch_regex") or "").strip() for rule in (rules or {}).values())


def resolve_batch_task_type(
    batch_name: str,
    fallback_task_type: str,
    rules: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[str, str, list[str]]:
    """Resolve a batch mapping, failing closed when mappings are absent or ambiguous."""
    mapping_rules = {
        task_type: str(rule.get("batch_regex") or "").strip()
        for task_type, rule in (rules or {}).items()
        if str(rule.get("batch_regex") or "").strip()
    }
    if not mapping_rules:
        return fallback_task_type, "inferred", []
    matches = [
        task_type
        for task_type, pattern in mapping_rules.items()
        if re.search(pattern, str(batch_name or ""), re.I)
    ]
    if len(matches) == 1:
        return matches[0], "mapped", matches
    if not matches:
        return fallback_task_type, "unmapped", []
    return fallback_task_type, "ambiguous", matches


def effective_tag_count_bounds(
    task_type: str,
    global_minimum: int | None,
    global_maximum: int | None,
    rules: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[int | None, int | None]:
    """Return an explicit task-type rule, or the global fallback bounds."""
    rule = rules.get(task_type) if rules else None
    if rule is None or (rule.get("min") is None and rule.get("max") is None):
        return global_minimum, global_maximum
    return rule.get("min"), rule.get("max")
