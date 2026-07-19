from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_SEARCH_TTL_SECONDS = 120
DEFAULT_WORKING_TTL_SECONDS = 12 * 60 * 60
MAX_BODY_BYTES = 32 * 1024


def utc_timestamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class LeaseStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS account_leases (
                    account_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    owner_label TEXT NOT NULL,
                    lease_token TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('searching', 'working')),
                    campaign_id TEXT,
                    task_id TEXT,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )

    @staticmethod
    def _public(row: sqlite3.Row | None, now: float) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "account_key": row["account_key"],
            "owner_id": row["owner_id"],
            "owner_label": row["owner_label"],
            "phase": row["phase"],
            "campaign_id": row["campaign_id"],
            "task_id": row["task_id"],
            "updated_at": utc_timestamp(float(row["updated_at"])),
            "expires_at": utc_timestamp(float(row["expires_at"])),
            "expires_in_seconds": max(0, round(float(row["expires_at"]) - now, 1)),
        }

    def acquire(
        self,
        *,
        account_key: str,
        owner_id: str,
        owner_label: str,
        lease_token: str,
        campaign_id: str | None,
        ttl_seconds: int = DEFAULT_SEARCH_TTL_SECONDS,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        expires_at = now + max(30, min(int(ttl_seconds), 300))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM account_leases WHERE account_key = ?",
                (account_key,),
            ).fetchone()
            if row is not None and float(row["expires_at"]) > now and row["lease_token"] != lease_token:
                connection.commit()
                return {"acquired": False, "lease": self._public(row, now)}
            connection.execute(
                """
                INSERT INTO account_leases (
                    account_key, owner_id, owner_label, lease_token, phase,
                    campaign_id, task_id, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, 'searching', ?, NULL, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    owner_label = excluded.owner_label,
                    lease_token = excluded.lease_token,
                    phase = 'searching',
                    campaign_id = excluded.campaign_id,
                    task_id = NULL,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (account_key, owner_id, owner_label, lease_token, campaign_id, now, expires_at),
            )
            row = connection.execute(
                "SELECT * FROM account_leases WHERE account_key = ?",
                (account_key,),
            ).fetchone()
            connection.commit()
        return {"acquired": True, "lease": self._public(row, now)}

    def renew(
        self,
        *,
        account_key: str,
        lease_token: str,
        ttl_seconds: int = DEFAULT_SEARCH_TTL_SECONDS,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        expires_at = now + max(30, min(int(ttl_seconds), 300))
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE account_leases
                SET updated_at = ?, expires_at = ?
                WHERE account_key = ? AND lease_token = ? AND phase = 'searching' AND expires_at > ?
                """,
                (now, expires_at, account_key, lease_token, now),
            )
            row = connection.execute(
                "SELECT * FROM account_leases WHERE account_key = ? AND expires_at > ?",
                (account_key, now),
            ).fetchone()
        return {"renewed": cursor.rowcount == 1, "lease": self._public(row, now)}

    def mark_working(
        self,
        *,
        account_key: str,
        lease_token: str,
        task_id: str,
        ttl_seconds: int = DEFAULT_WORKING_TTL_SECONDS,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else now
        expires_at = now + max(300, min(int(ttl_seconds), 24 * 60 * 60))
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE account_leases
                SET phase = 'working', task_id = ?, updated_at = ?, expires_at = ?
                WHERE account_key = ? AND lease_token = ? AND phase = 'searching' AND expires_at > ?
                """,
                (task_id, now, expires_at, account_key, lease_token, now),
            )
            row = connection.execute(
                "SELECT * FROM account_leases WHERE account_key = ? AND expires_at > ?",
                (account_key, now),
            ).fetchone()
        return {"updated": cursor.rowcount == 1, "lease": self._public(row, now)}

    def release(self, *, account_key: str, lease_token: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM account_leases WHERE account_key = ? AND lease_token = ?",
                (account_key, lease_token),
            )
        return {"released": cursor.rowcount == 1}

    def status(self, *, account_key: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM account_leases WHERE account_key = ? AND expires_at > ?",
                (account_key, now),
            ).fetchone()
        return {"busy": row is not None, "lease": self._public(row, now)}


def required_text(payload: dict[str, Any], field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ValueError(f"Missing {field}.")
    if len(value) > 500:
        raise ValueError(f"{field} is too long.")
    return value


def make_handler(store: LeaseStore, service_token: str) -> type[BaseHTTPRequestHandler]:
    class CoordinationHandler(BaseHTTPRequestHandler):
        server_version = "FeatherCoordination/1.0"

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            supplied = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
            return bool(supplied) and hmac.compare_digest(supplied, service_token)

        def _json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 1 or length > MAX_BODY_BYTES:
                raise ValueError("Invalid request body size.")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object.")
            return value

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._write(200, {"ok": True})
                return
            self._write(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            if not self._authorized():
                self._write(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                payload = self._json_body()
                account_key = required_text(payload, "account_key")
                if self.path == "/v1/leases/acquire":
                    result = store.acquire(
                        account_key=account_key,
                        owner_id=required_text(payload, "owner_id"),
                        owner_label=required_text(payload, "owner_label"),
                        lease_token=required_text(payload, "lease_token"),
                        campaign_id=str(payload.get("campaign_id") or "").strip() or None,
                        ttl_seconds=int(payload.get("ttl_seconds") or DEFAULT_SEARCH_TTL_SECONDS),
                    )
                    self._write(200 if result["acquired"] else 409, {"ok": result["acquired"], **result})
                    return
                if self.path == "/v1/leases/renew":
                    result = store.renew(
                        account_key=account_key,
                        lease_token=required_text(payload, "lease_token"),
                        ttl_seconds=int(payload.get("ttl_seconds") or DEFAULT_SEARCH_TTL_SECONDS),
                    )
                    self._write(200 if result["renewed"] else 409, {"ok": result["renewed"], **result})
                    return
                if self.path == "/v1/leases/working":
                    result = store.mark_working(
                        account_key=account_key,
                        lease_token=required_text(payload, "lease_token"),
                        task_id=required_text(payload, "task_id"),
                        ttl_seconds=int(payload.get("ttl_seconds") or DEFAULT_WORKING_TTL_SECONDS),
                    )
                    self._write(200 if result["updated"] else 409, {"ok": result["updated"], **result})
                    return
                if self.path == "/v1/leases/release":
                    result = store.release(
                        account_key=account_key,
                        lease_token=required_text(payload, "lease_token"),
                    )
                    self._write(200, {"ok": True, **result})
                    return
                if self.path == "/v1/leases/status":
                    self._write(200, {"ok": True, **store.status(account_key=account_key)})
                    return
                self._write(404, {"ok": False, "error": "not found"})
            except (ValueError, json.JSONDecodeError) as exc:
                self._write(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                print(
                    f"coordination server error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                self._write(500, {"ok": False, "error": "internal server error"})

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} {format % args}", flush=True)

    return CoordinationHandler


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Shared Feather account coordination service.")
    parser.add_argument("--host", default=os.environ.get("FEATHER_COORDINATOR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FEATHER_COORDINATOR_PORT", "8787")))
    parser.add_argument(
        "--db",
        default=os.environ.get("FEATHER_COORDINATOR_DB", "data/feather-coordination.sqlite3"),
    )
    parser.add_argument("--token", default=os.environ.get("FEATHER_COORDINATOR_TOKEN"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = str(args.token or "").strip()
    if len(token) < 24:
        raise SystemExit("Set FEATHER_COORDINATOR_TOKEN to a random value of at least 24 characters.")
    store = LeaseStore(args.db)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, token))
    print(f"Feather coordination server listening on http://{args.host}:{args.port}", flush=True)
    print(f"Database: {Path(args.db).resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
