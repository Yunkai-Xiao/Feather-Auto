from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


DEFAULT_SEARCH_TTL_SECONDS = 120
DEFAULT_WORKING_TTL_SECONDS = 12 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 5


class CoordinationError(RuntimeError):
    pass


class LeaseUnavailable(CoordinationError):
    def __init__(self, lease: dict[str, Any] | None) -> None:
        self.lease = lease or {}
        owner = self.lease.get("owner_label") or "another operator"
        phase = self.lease.get("phase") or "busy"
        super().__init__(f"Feather account is already {phase} under {owner}.")


def default_owner_label() -> str:
    return os.environ.get("FEATHER_COORDINATION_OWNER", "").strip() or f"{getpass.getuser()}@{platform.node()}"


def account_key_for_user(user: dict[str, Any]) -> str:
    identity = str(user.get("id") or user.get("email") or "").strip().lower()
    if not identity:
        raise CoordinationError("Feather whoami response did not include an account id or email.")
    return f"feather:{identity}"


@dataclass(frozen=True)
class CoordinationConfig:
    url: str
    service_token: str
    owner_label: str
    state_file: str | None = None
    search_ttl_seconds: int = DEFAULT_SEARCH_TTL_SECONDS
    working_ttl_seconds: int = DEFAULT_WORKING_TTL_SECONDS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    @property
    def enabled(self) -> bool:
        return bool(self.url.strip())


class CoordinationLease:
    def __init__(self, config: CoordinationConfig, account_key: str, campaign_id: str) -> None:
        self.config = config
        self.account_key = account_key
        self.campaign_id = campaign_id
        self.owner_id = uuid.uuid4().hex
        self.lease_token = uuid.uuid4().hex
        self.phase = "new"
        self.lease: dict[str, Any] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.config.url.rstrip("/") + path
        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.config.service_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.config.timeout_seconds,
            )
        except requests.exceptions.RequestException as exc:
            raise CoordinationError(f"Coordination server request failed: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise CoordinationError(f"Coordination server returned HTTP {response.status_code} without JSON.") from exc
        if not isinstance(data, dict):
            raise CoordinationError("Coordination server returned an invalid response.")
        if response.status_code >= 500:
            raise CoordinationError(str(data.get("error") or f"Coordination server HTTP {response.status_code}"))
        if response.status_code in (401, 403):
            raise CoordinationError("Coordination server rejected FEATHER_COORDINATION_TOKEN.")
        return data

    def acquire(self) -> dict[str, Any]:
        data = self._post(
            "/v1/leases/acquire",
            {
                "account_key": self.account_key,
                "owner_id": self.owner_id,
                "owner_label": self.config.owner_label,
                "lease_token": self.lease_token,
                "campaign_id": self.campaign_id,
                "ttl_seconds": self.config.search_ttl_seconds,
            },
        )
        if not data.get("acquired"):
            raise LeaseUnavailable(data.get("lease"))
        self.phase = "searching"
        self.lease = dict(data.get("lease") or {})
        self._thread = threading.Thread(target=self._heartbeat, name="FeatherCoordinationLease", daemon=True)
        self._thread.start()
        return self.lease

    def _renew(self) -> dict[str, Any]:
        data = self._post(
            "/v1/leases/renew",
            {
                "account_key": self.account_key,
                "lease_token": self.lease_token,
                "ttl_seconds": self.config.search_ttl_seconds,
            },
        )
        if not data.get("renewed"):
            raise LeaseUnavailable(data.get("lease"))
        self.lease = dict(data.get("lease") or {})
        self._last_error = None
        return self.lease

    def _heartbeat(self) -> None:
        interval = max(10.0, self.config.search_ttl_seconds / 3)
        while not self._stop_event.wait(interval):
            if self.phase != "searching":
                return
            try:
                self._renew()
            except CoordinationError as exc:
                self._last_error = str(exc)

    def ensure_owned(self) -> dict[str, Any]:
        if self.phase != "searching":
            raise CoordinationError("Coordination lease is not in searching state.")
        return self._renew()

    def mark_working(self, task_id: str) -> dict[str, Any]:
        self.ensure_owned()
        data = self._post(
            "/v1/leases/working",
            {
                "account_key": self.account_key,
                "lease_token": self.lease_token,
                "task_id": task_id,
                "ttl_seconds": self.config.working_ttl_seconds,
            },
        )
        if not data.get("updated"):
            raise LeaseUnavailable(data.get("lease"))
        self.phase = "working"
        self.lease = dict(data.get("lease") or {})
        self._stop_event.set()
        self._save_state(task_id)
        return self.lease

    def _save_state(self, task_id: str) -> None:
        if not self.config.state_file:
            return
        path = Path(self.config.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "url": self.config.url,
                    "account_key": self.account_key,
                    "owner_id": self.owner_id,
                    "owner_label": self.config.owner_label,
                    "lease_token": self.lease_token,
                    "task_id": task_id,
                    "lease": self.lease,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def release(self) -> bool:
        data = self._post(
            "/v1/leases/release",
            {"account_key": self.account_key, "lease_token": self.lease_token},
        )
        released = bool(data.get("released"))
        if released:
            self.phase = "released"
            if self.config.state_file:
                Path(self.config.state_file).unlink(missing_ok=True)
        return released

    def close(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        if self.phase == "searching":
            try:
                self.release()
            except CoordinationError:
                pass


def release_saved_lease(state_file: str | Path, service_token: str) -> dict[str, Any]:
    path = Path(state_file)
    if not path.exists():
        return {"released": False, "reason": "no saved working lease"}
    state = json.loads(path.read_text(encoding="utf-8"))
    config = CoordinationConfig(
        url=str(state.get("url") or ""),
        service_token=service_token,
        owner_label=str(state.get("owner_label") or default_owner_label()),
    )
    lease = CoordinationLease(config, str(state["account_key"]), "")
    lease.owner_id = str(state.get("owner_id") or "")
    lease.lease_token = str(state["lease_token"])
    lease.phase = "working"
    released = lease.release()
    if released:
        path.unlink(missing_ok=True)
    return {"released": released, "task_id": state.get("task_id"), "account_key": state.get("account_key")}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage a saved Feather coordination lease.")
    parser.add_argument("command", choices=["release"])
    parser.add_argument("--state-file", default=os.environ.get("FEATHER_COORDINATION_STATE_FILE", "outputs/coordination_lease.json"))
    parser.add_argument("--token", default=os.environ.get("FEATHER_COORDINATION_TOKEN"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = str(args.token or "").strip()
    if not token:
        raise SystemExit("Set FEATHER_COORDINATION_TOKEN before releasing the lease.")
    result = release_saved_lease(args.state_file, token)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("released") else 1


if __name__ == "__main__":
    raise SystemExit(main())
