from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class TaskHistoryStore:
    """Persist one complete task-count snapshot per campaign/filter time bucket."""

    def __init__(self, path: str | Path, interval_minutes: int = 30, retention_days: int = 30) -> None:
        if interval_minutes < 1:
            raise ValueError("interval_minutes must be >= 1")
        if retention_days < 1:
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
            "version": 1,
            "interval_minutes": self.interval_minutes,
            "retention_days": self.retention_days,
            "records": records,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        temp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(self.path)

    def record_status(self, status: dict[str, Any], observed_at: datetime | None = None) -> bool:
        if status.get("history_sample_complete") is not True:
            return False
        campaign_id = str(status.get("campaign_id") or "").strip()
        if not campaign_id:
            return False
        try:
            total_tasks = int(status["total_unclaimed_count"])
            matching_tasks = int(status["matching_count"])
        except (KeyError, TypeError, ValueError):
            return False
        if total_tasks < 0 or matching_tasks < 0:
            return False

        observed_at = observed_at or datetime.now().astimezone()
        if observed_at.tzinfo is None:
            observed_at = observed_at.astimezone()
        bucket_start = self._bucket_start(observed_at)
        filter_fields = self._filter_fields(status)
        filter_key = self._filter_key(status)
        record = {
            "bucket_start": bucket_start.isoformat(),
            "observed_at": observed_at.isoformat(),
            "campaign_id": campaign_id,
            **filter_fields,
            "filter_key": filter_key,
            "total_tasks": total_tasks,
            "matching_tasks": matching_tasks,
        }

        with self._lock:
            records = self._read_records_unlocked()
            duplicate = any(
                existing.get("campaign_id") == campaign_id
                and existing.get("bucket_start") == record["bucket_start"]
                and existing.get("filter_key") == filter_key
                for existing in records
            )
            if duplicate:
                return False

            cutoff = observed_at - timedelta(days=self.retention_days)
            retained: list[dict[str, Any]] = []
            for existing in records:
                try:
                    existing_time = datetime.fromisoformat(str(existing.get("observed_at") or ""))
                    if existing_time.tzinfo is None:
                        existing_time = existing_time.astimezone()
                except ValueError:
                    continue
                if existing_time >= cutoff:
                    retained.append(existing)
            retained.append(record)
            retained.sort(key=lambda item: str(item.get("observed_at") or ""))
            self._write_records_unlocked(retained)
        return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            records = self._read_records_unlocked()
        records.sort(key=lambda item: str(item.get("observed_at") or ""))
        return {
            "interval_minutes": self.interval_minutes,
            "retention_days": self.retention_days,
            "records": records,
        }
