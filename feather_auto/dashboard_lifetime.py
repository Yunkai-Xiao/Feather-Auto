from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class DashboardLeaseRegistry:
    """Track live dashboard pages and decide when the server may shut down."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        close_grace_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.close_grace_seconds = close_grace_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: dict[str, float] = {}
        self._auto_shutdown = False
        self._seen_client = False
        self._empty_since: float | None = None
        self._shutdown_triggered = False

    @staticmethod
    def _validated_client_id(client_id: str) -> str:
        client_id = str(client_id or "").strip()
        if not client_id:
            raise ValueError("Missing dashboard client id.")
        if len(client_id) > 128:
            raise ValueError("Dashboard client id is too long.")
        return client_id

    def heartbeat(self, client_id: str, auto_shutdown: bool) -> dict[str, Any]:
        client_id = self._validated_client_id(client_id)
        now = self._clock()
        with self._lock:
            self._clients[client_id] = now
            self._auto_shutdown = bool(auto_shutdown)
            self._seen_client = True
            self._empty_since = None
            return self._snapshot_unlocked(now)

    def disconnect(self, client_id: str, auto_shutdown: bool | None = None) -> dict[str, Any]:
        client_id = self._validated_client_id(client_id)
        now = self._clock()
        with self._lock:
            if client_id not in self._clients:
                return self._snapshot_unlocked(now)
            if auto_shutdown is not None:
                self._auto_shutdown = bool(auto_shutdown)
            self._clients.pop(client_id)
            if self._seen_client and self._auto_shutdown and not self._clients:
                self._empty_since = now
            return self._snapshot_unlocked(now)

    def has_client(self, client_id: str) -> bool:
        client_id = self._validated_client_id(client_id)
        with self._lock:
            return client_id in self._clients

    def poll(self) -> bool:
        """Expire dead pages and return True once when shutdown should begin."""
        now = self._clock()
        with self._lock:
            expired_before = now - self.ttl_seconds
            expired = [client_id for client_id, seen_at in self._clients.items() if seen_at <= expired_before]
            for client_id in expired:
                self._clients.pop(client_id, None)

            if not self._auto_shutdown:
                self._empty_since = None
                return False
            if self._clients or not self._seen_client or self._shutdown_triggered:
                return False
            if self._empty_since is None:
                self._empty_since = now
                return False
            if now - self._empty_since < self.close_grace_seconds:
                return False

            self._shutdown_triggered = True
            return True

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            return self._snapshot_unlocked(now)

    def _snapshot_unlocked(self, now: float) -> dict[str, Any]:
        shutdown_in = None
        if self._empty_since is not None and self._auto_shutdown and not self._clients:
            shutdown_in = max(0.0, self.close_grace_seconds - (now - self._empty_since))
        return {
            "connected_clients": len(self._clients),
            "auto_shutdown": self._auto_shutdown,
            "ttl_seconds": self.ttl_seconds,
            "close_grace_seconds": self.close_grace_seconds,
            "shutdown_pending": shutdown_in is not None,
            "shutdown_in_seconds": round(shutdown_in, 1) if shutdown_in is not None else None,
        }
