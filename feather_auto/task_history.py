from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .tag_rules import effective_tag_count_bounds, normalize_tag_count_rules, resolve_batch_task_type


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


class TaskHistoryStore:
    """Persist summed complete task-count polls per campaign/filter time bucket."""

    def __init__(
        self,
        path: str | Path,
        interval_minutes: int = 30,
        retention_days: int | None = None,
    ) -> None:
        if interval_minutes < 1:
            raise ValueError("interval_minutes must be >= 1")
        if retention_days is not None and retention_days < 1:
            raise ValueError("retention_days must be >= 1")
        self.path = Path(path)
        self.interval_minutes = interval_minutes
        self.retention_days = retention_days
        self._lock = threading.RLock()

    @staticmethod
    def _filter_fields(status: dict[str, Any]) -> dict[str, Any]:
        tag_filter = status.get("tag_count_filter")
        tag_filter = tag_filter if isinstance(tag_filter, dict) else {}
        return {
            "batch_regex": str(status.get("batch_regex") or ""),
            "tag_count_min": tag_filter.get("min"),
            "tag_count_max": tag_filter.get("max"),
            "tag_count_rules": normalize_tag_count_rules(status.get("tag_count_rules")),
        }

    @classmethod
    def _filter_key(cls, status: dict[str, Any]) -> str:
        return json.dumps(cls._filter_fields(status), sort_keys=True, separators=(",", ":"))

    def _bucket_start(self, observed_at: datetime) -> datetime:
        minute_of_day = observed_at.hour * 60 + observed_at.minute
        bucket_minute = minute_of_day - (minute_of_day % self.interval_minutes)
        return observed_at.replace(
            hour=bucket_minute // 60,
            minute=bucket_minute % 60,
            second=0,
            microsecond=0,
        )

    def _read_records_unlocked(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return []
        records = payload.get("records") if isinstance(payload, dict) else None
        return [dict(record) for record in records if isinstance(record, dict)] if isinstance(records, list) else []

    def _write_records_unlocked(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 3,
            "aggregation": "sum",
            "interval_minutes": self.interval_minutes,
            "retention_days": self.retention_days,
            "retention": "forever" if self.retention_days is None else f"{self.retention_days}_days",
            "records": records,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        temp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(self.path)

    def record_status(self, status: dict[str, Any], observed_at: datetime | None = None) -> bool:
        if status.get("history_sample_complete") is not True:
            return False
        if status.get("claim_history_sample_complete") is not True:
            return False
        campaign_id = str(status.get("campaign_id") or "").strip()
        if not campaign_id:
            return False
        try:
            total_tasks = int(status["total_unclaimed_count"])
            matching_tasks = int(status["matching_count"])
            previously_claimed_tasks = int(status["previously_claimed_count"])
        except (KeyError, TypeError, ValueError):
            return False
        if total_tasks < 0 or matching_tasks < 0 or previously_claimed_tasks < 0:
            return False

        observed_at = observed_at or datetime.now().astimezone()
        if observed_at.tzinfo is None:
            observed_at = observed_at.astimezone()
        bucket_start = self._bucket_start(observed_at)
        filter_fields = self._filter_fields(status)
        filter_key = self._filter_key(status)
        sample_id = str(status.get("last_poll") or "")
        record = {
            "bucket_start": bucket_start.isoformat(),
            "observed_at": observed_at.isoformat(),
            "campaign_id": campaign_id,
            **filter_fields,
            "filter_key": filter_key,
            "total_tasks": total_tasks,
            "matching_tasks": matching_tasks,
            "previously_claimed_tasks": previously_claimed_tasks,
            "last_sample_id": sample_id,
        }

        with self._lock:
            records = self._read_records_unlocked()
            matched_record = next(
                (
                    history_record
                    for history_record in records
                    if history_record.get("campaign_id") == campaign_id
                    and history_record.get("bucket_start") == record["bucket_start"]
                    and history_record.get("filter_key") == filter_key
                ),
                None,
            )
            if matched_record is not None:
                if total_tasks == 0 and matching_tasks == 0 and previously_claimed_tasks == 0:
                    return False
                if sample_id and matched_record.get("last_sample_id") == sample_id:
                    return False
                try:
                    existing_total = int(matched_record.get("total_tasks") or 0)
                    existing_matching = int(matched_record.get("matching_tasks") or 0)
                    existing_previously_claimed = int(
                        matched_record.get("previously_claimed_tasks") or 0
                    )
                except (TypeError, ValueError):
                    existing_total = 0
                    existing_matching = 0
                    existing_previously_claimed = 0
                matched_record["total_tasks"] = existing_total + total_tasks
                matched_record["matching_tasks"] = existing_matching + matching_tasks
                matched_record["previously_claimed_tasks"] = (
                    existing_previously_claimed + previously_claimed_tasks
                )
                matched_record["observed_at"] = observed_at.isoformat()
                matched_record["last_sample_id"] = sample_id

            retained: list[dict[str, Any]] = []
            for history_record in records:
                try:
                    existing_time = datetime.fromisoformat(str(history_record.get("observed_at") or ""))
                    if existing_time.tzinfo is None:
                        existing_time = existing_time.astimezone()
                except ValueError:
                    continue
                if self.retention_days is None:
                    retained.append(history_record)
                    continue
                cutoff = observed_at - timedelta(days=self.retention_days)
                if existing_time >= cutoff:
                    retained.append(history_record)
            if matched_record is None:
                retained.append(record)
            retained.sort(key=lambda item: str(item.get("observed_at") or ""))
            self._write_records_unlocked(retained)
        return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            records = self._read_records_unlocked()
        records.sort(key=lambda item: str(item.get("observed_at") or ""))
        return {
            "aggregation": "sum",
            "interval_minutes": self.interval_minutes,
            "retention_days": self.retention_days,
            "retention": "forever" if self.retention_days is None else f"{self.retention_days}_days",
            "records": records,
        }


class TaskObservationStore:
    """Keep every poll and task observation in an append-only SQLite history."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._history_revision = 0
        self._history_cache: dict[tuple[int, int | None], tuple[int, dict[str, Any]]] = {}

    @staticmethod
    def _observed_at(status: dict[str, Any], observed_at: datetime | None) -> datetime:
        if observed_at is None:
            raw = str(status.get("updated_at") or "").strip()
            try:
                observed_at = datetime.fromisoformat(raw) if raw else None
            except ValueError:
                observed_at = None
        observed_at = observed_at or datetime.now().astimezone()
        if observed_at.tzinfo is None:
            observed_at = observed_at.astimezone()
        return observed_at

    def _connect_unlocked(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS poll_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                observed_epoch REAL NOT NULL,
                campaign_id TEXT NOT NULL,
                filter_key TEXT NOT NULL,
                batch_regex TEXT NOT NULL,
                tag_count_min INTEGER,
                tag_count_max INTEGER,
                source TEXT NOT NULL,
                complete INTEGER NOT NULL,
                reported_unclaimed_count INTEGER,
                scanned_unclaimed_count INTEGER NOT NULL,
                eligible_count INTEGER NOT NULL,
                UNIQUE(campaign_id, filter_key, poll_id)
            );
            CREATE TABLE IF NOT EXISTS task_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                observed_epoch REAL NOT NULL,
                campaign_id TEXT NOT NULL,
                filter_key TEXT NOT NULL,
                task_id TEXT NOT NULL,
                tag_count INTEGER,
                task_type TEXT NOT NULL,
                batch_name TEXT NOT NULL,
                eligible INTEGER NOT NULL,
                previously_claimed INTEGER,
                UNIQUE(campaign_id, filter_key, poll_id, task_id)
            );
            CREATE INDEX IF NOT EXISTS idx_poll_samples_range
                ON poll_samples(campaign_id, filter_key, observed_epoch);
            CREATE INDEX IF NOT EXISTS idx_task_observations_range
                ON task_observations(campaign_id, filter_key, observed_epoch, task_id);
            CREATE INDEX IF NOT EXISTS idx_poll_samples_campaign_range
                ON poll_samples(campaign_id, observed_epoch);
            CREATE INDEX IF NOT EXISTS idx_task_observations_campaign_range
                ON task_observations(campaign_id, observed_epoch, task_id);
            """
        )
        observation_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(task_observations)")
        }
        if "previously_claimed" not in observation_columns:
            connection.execute(
                "ALTER TABLE task_observations ADD COLUMN previously_claimed INTEGER"
            )
            connection.commit()
        return connection

    def record_status(self, status: dict[str, Any], observed_at: datetime | None = None) -> bool:
        distributions = status.get("task_distributions")
        observations = status.get("task_observations")
        if not isinstance(distributions, dict) or not isinstance(observations, list):
            return False
        campaign_id = str(status.get("campaign_id") or "").strip()
        poll_id = str(status.get("poll_observation_id") or "").strip()
        if not campaign_id or not poll_id:
            return False

        observed_at = self._observed_at(status, observed_at)
        observed_iso = observed_at.isoformat()
        observed_epoch = observed_at.timestamp()
        filter_fields = TaskHistoryStore._filter_fields(status)
        filter_key = TaskHistoryStore._filter_key(status)
        source = str(distributions.get("source") or "unknown")
        complete = int(distributions.get("complete") is True)
        try:
            reported_count = distributions.get("reported_unclaimed_count")
            reported_count = None if reported_count is None else int(reported_count)
            scanned_count = int(distributions.get("scanned_unclaimed_count") or 0)
            eligible_count = int(distributions.get("eligible_count") or 0)
        except (TypeError, ValueError):
            return False

        rows: list[tuple[Any, ...]] = []
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            task_id = str(observation.get("task_id") or "").strip()
            if not task_id:
                continue
            tag_count = observation.get("tag_count")
            try:
                tag_count = None if tag_count is None else int(tag_count)
            except (TypeError, ValueError):
                tag_count = None
            raw_previously_claimed = observation.get("previously_claimed")
            previously_claimed = (
                None
                if raw_previously_claimed is None
                else int(raw_previously_claimed is True)
            )
            rows.append(
                (
                    poll_id,
                    observed_iso,
                    observed_epoch,
                    campaign_id,
                    filter_key,
                    task_id,
                    tag_count,
                    str(observation.get("task_type") or "Other"),
                    str(observation.get("batch_name") or ""),
                    int(observation.get("eligible") is True),
                    previously_claimed,
                )
            )

        with self._lock:
            connection = self._connect_unlocked()
            try:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO poll_samples (
                        poll_id, observed_at, observed_epoch, campaign_id, filter_key,
                        batch_regex, tag_count_min, tag_count_max, source, complete,
                        reported_unclaimed_count, scanned_unclaimed_count, eligible_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        poll_id,
                        observed_iso,
                        observed_epoch,
                        campaign_id,
                        filter_key,
                        filter_fields["batch_regex"],
                        filter_fields["tag_count_min"],
                        filter_fields["tag_count_max"],
                        source,
                        complete,
                        reported_count,
                        scanned_count,
                        eligible_count,
                    ),
                )
                if rows:
                    connection.executemany(
                        """
                        INSERT OR IGNORE INTO task_observations (
                            poll_id, observed_at, observed_epoch, campaign_id, filter_key,
                            task_id, tag_count, task_type, batch_name, eligible,
                            previously_claimed
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                connection.commit()
                inserted = cursor.rowcount > 0
                if inserted:
                    self._history_revision += 1
                    self._history_cache.clear()
                return inserted
            finally:
                connection.close()

    def history_snapshot(
        self,
        *,
        interval_minutes: int = 30,
        retention_days: int | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Count distinct task IDs in each time bucket and saved filter series."""
        if interval_minutes < 1:
            raise ValueError("interval_minutes must be >= 1")
        if retention_days is not None and retention_days < 1:
            raise ValueError("retention_days must be >= 1 or None")

        now = observed_at or datetime.now().astimezone()
        if now.tzinfo is None:
            now = now.astimezone()
        interval_seconds = interval_minutes * 60
        cache_key = (interval_minutes, retention_days)
        if observed_at is None:
            with self._lock:
                cached = self._history_cache.get(cache_key)
                if cached and cached[0] == self._history_revision:
                    payload = cached[1]
                    return {**payload, "records": [dict(record) for record in payload["records"]]}
        cutoff = None if retention_days is None else now.timestamp() - retention_days * 86400
        where = ""
        where_parameters: list[Any] = []
        if cutoff is not None:
            where = "WHERE observed_epoch >= ?"
            where_parameters.append(cutoff)

        poll_rows: list[tuple[Any, ...]] = []
        task_rows: list[tuple[Any, ...]] = []
        snapshot_revision = self._history_revision
        with self._lock:
            if self.path.exists():
                connection = self._connect_unlocked()
                try:
                    poll_rows = connection.execute(
                        f"""
                        SELECT campaign_id, filter_key,
                               CAST(observed_epoch / ? AS INTEGER) AS bucket_key,
                               MIN(observed_at), MAX(observed_at), COUNT(*)
                        FROM poll_samples
                        {where}
                        GROUP BY campaign_id, filter_key, bucket_key
                        """,
                        [interval_seconds, *where_parameters],
                    ).fetchall()
                    task_rows = connection.execute(
                        f"""
                        SELECT campaign_id, filter_key,
                               CAST(observed_epoch / ? AS INTEGER) AS bucket_key,
                               COUNT(DISTINCT task_id),
                               COUNT(DISTINCT CASE WHEN eligible = 1 THEN task_id END),
                               SUM(CASE WHEN previously_claimed IS NOT NULL THEN 1 ELSE 0 END),
                               COUNT(DISTINCT CASE WHEN previously_claimed = 1 THEN task_id END)
                        FROM task_observations
                        {where}
                        GROUP BY campaign_id, filter_key, bucket_key
                        """,
                        [interval_seconds, *where_parameters],
                    ).fetchall()
                finally:
                    connection.close()

        task_counts = {
            (str(campaign_id), str(filter_key), int(bucket_key)): (
                int(total_tasks or 0),
                int(eligible_tasks or 0),
                int(claim_history_observations or 0),
                int(previously_claimed_tasks or 0),
            )
            for (
                campaign_id,
                filter_key,
                bucket_key,
                total_tasks,
                eligible_tasks,
                claim_history_observations,
                previously_claimed_tasks,
            ) in task_rows
        }
        records: list[dict[str, Any]] = []
        for campaign_id, filter_key, bucket_key, first_at, last_at, sample_count in poll_rows:
            try:
                filter_fields = json.loads(str(filter_key))
            except (TypeError, json.JSONDecodeError):
                filter_fields = {}
            if not isinstance(filter_fields, dict):
                filter_fields = {}
            try:
                last_datetime = datetime.fromisoformat(str(last_at))
                bucket_timezone = last_datetime.tzinfo or now.tzinfo
            except ValueError:
                bucket_timezone = now.tzinfo
            total_tasks, eligible_tasks, known_claim_rows, claimed_tasks = task_counts.get(
                (str(campaign_id), str(filter_key), int(bucket_key)),
                (0, 0, 0, 0),
            )
            records.append(
                {
                    "bucket_start": datetime.fromtimestamp(
                        int(bucket_key) * interval_seconds,
                        tz=bucket_timezone,
                    ).isoformat(),
                    "observed_at": str(last_at),
                    "first_observed_at": str(first_at),
                    "campaign_id": str(campaign_id),
                    "batch_regex": str(filter_fields.get("batch_regex") or ""),
                    "tag_count_min": filter_fields.get("tag_count_min"),
                    "tag_count_max": filter_fields.get("tag_count_max"),
                    "tag_count_rules": normalize_tag_count_rules(
                        filter_fields.get("tag_count_rules")
                    ),
                    "filter_key": str(filter_key),
                    "total_tasks": total_tasks,
                    "matching_tasks": eligible_tasks,
                    "previously_claimed_tasks": claimed_tasks if known_claim_rows else None,
                    "sample_count": int(sample_count or 0),
                }
            )
        records.sort(key=lambda item: str(item.get("observed_at") or ""))
        payload = {
            "version": 1,
            "aggregation": "unique_tasks",
            "interval_minutes": interval_minutes,
            "retention_days": retention_days,
            "retention": "forever" if retention_days is None else f"{retention_days}_days",
            "records": records,
        }
        if observed_at is None:
            with self._lock:
                if snapshot_revision == self._history_revision:
                    self._history_cache[cache_key] = (snapshot_revision, payload)
        return {**payload, "records": [dict(record) for record in records]}

    def distribution(
        self,
        *,
        campaign_id: str,
        batch_regex: str = "",
        tag_count_min: int | None = None,
        tag_count_max: int | None = None,
        tag_count_rules: dict[str, dict[str, Any]] | None = None,
        tag_task_type: str | None = None,
        range_minutes: int | None = 30,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        if range_minutes is not None and range_minutes < 1:
            raise ValueError("range_minutes must be >= 1 or None")
        now = observed_at or datetime.now().astimezone()
        if now.tzinfo is None:
            now = now.astimezone()
        cutoff = None if range_minutes is None else now.timestamp() - range_minutes * 60
        try:
            batch_pattern = re.compile(batch_regex, re.I) if batch_regex.strip() else None
        except re.error as exc:
            raise ValueError(f"Invalid batch regex: {exc}") from exc
        selected_tag_task_type = str(tag_task_type or "").strip()
        if selected_tag_task_type.lower() == "all":
            selected_tag_task_type = ""
        normalized_tag_rules = normalize_tag_count_rules(tag_count_rules)

        # Raw observations belong to a campaign, not to the monitor filter that
        # happened to be active when they were captured. Filters are applied
        # below at query time so changing them never starts a new history series.
        where = "campaign_id = ?"
        parameters: list[Any] = [campaign_id]
        if cutoff is not None:
            where += " AND observed_epoch >= ?"
            parameters.append(cutoff)

        poll_count = 0
        complete_poll_count = 0
        first_observed_at = None
        last_observed_at = None
        observation_count = 0
        task_rows: list[tuple[Any, ...]] = []
        with self._lock:
            if self.path.exists():
                connection = self._connect_unlocked()
                try:
                    poll_row = connection.execute(
                        f"""
                        SELECT COUNT(*), COALESCE(SUM(complete), 0),
                               MIN(observed_at), MAX(observed_at)
                        FROM poll_samples WHERE {where}
                        """,
                        parameters,
                    ).fetchone()
                    if poll_row:
                        poll_count = int(poll_row[0] or 0)
                        complete_poll_count = int(poll_row[1] or 0)
                        first_observed_at = poll_row[2]
                        last_observed_at = poll_row[3]
                    observation_row = connection.execute(
                        f"SELECT COUNT(*) FROM task_observations WHERE {where}",
                        parameters,
                    ).fetchone()
                    observation_count = int(observation_row[0] or 0) if observation_row else 0
                    task_rows = connection.execute(
                        f"""
                        SELECT latest.task_id, observation.tag_count,
                               observation.task_type, observation.batch_name
                        FROM (
                            SELECT task_id, MAX(id) AS latest_id
                            FROM task_observations
                            WHERE {where}
                            GROUP BY task_id
                        ) AS latest
                        JOIN task_observations AS observation
                          ON observation.id = latest.latest_id
                        """,
                        parameters,
                    ).fetchall()
                finally:
                    connection.close()

        tag_counts: dict[int | None, dict[str, Any]] = {}
        type_counts = {
            label: {"label": label, "unclaimed": 0, "eligible": 0}
            for label in TASK_TYPE_LABELS
        }
        eligible_task_count = 0
        tag_unique_task_count = 0
        for _task_id, tag_count, task_type, batch_name in task_rows:
            label, mapping_state, _mapping_matches = resolve_batch_task_type(
                str(batch_name or ""),
                str(task_type or "Other"),
                normalized_tag_rules,
            )
            effective_min, effective_max = effective_tag_count_bounds(
                label,
                tag_count_min,
                tag_count_max,
                normalized_tag_rules,
            )
            eligible = True
            if mapping_state in {"unmapped", "ambiguous"}:
                eligible = False
            if batch_pattern and not batch_pattern.search(str(batch_name or "")):
                eligible = False
            if effective_min is not None and (tag_count is None or tag_count < effective_min):
                eligible = False
            if effective_max is not None and (tag_count is None or tag_count > effective_max):
                eligible = False
            if eligible:
                eligible_task_count += 1

            if label not in type_counts:
                type_counts[label] = {"label": label, "unclaimed": 0, "eligible": 0}
            type_counts[label]["unclaimed"] += 1
            if eligible:
                type_counts[label]["eligible"] += 1

            if selected_tag_task_type and label != selected_tag_task_type:
                continue
            tag_unique_task_count += 1
            if tag_count not in tag_counts:
                tag_counts[tag_count] = {
                    "tag_count": tag_count,
                    "label": "Unknown" if tag_count is None else str(tag_count),
                    "unclaimed": 0,
                    "eligible": 0,
                }
            tag_counts[tag_count]["unclaimed"] += 1
            if eligible:
                tag_counts[tag_count]["eligible"] += 1

        type_labels = list(TASK_TYPE_LABELS)
        if "Other" in type_counts:
            type_labels.append("Other")
        unique_task_count = len(task_rows)
        return {
            "historical": True,
            "complete": poll_count > 0 and complete_poll_count == poll_count,
            "source": "history",
            "range_minutes": range_minutes,
            "range_start": None if cutoff is None else datetime.fromtimestamp(cutoff, tz=now.tzinfo).isoformat(),
            "range_end": now.isoformat(),
            "poll_count": poll_count,
            "complete_poll_count": complete_poll_count,
            "partial_poll_count": poll_count - complete_poll_count,
            "first_observed_at": first_observed_at,
            "last_observed_at": last_observed_at,
            "observation_count": observation_count,
            "unique_task_count": unique_task_count,
            "tag_unique_task_count": tag_unique_task_count,
            "tag_task_type": selected_tag_task_type or None,
            "scanned_unclaimed_count": unique_task_count,
            "reported_unclaimed_count": None,
            "eligible_count": eligible_task_count,
            "tag_counts": sorted(
                tag_counts.values(),
                key=lambda item: (
                    item["tag_count"] is None,
                    item["tag_count"] if item["tag_count"] is not None else 0,
                ),
            ),
            "task_types": [type_counts[label] for label in type_labels],
        }
