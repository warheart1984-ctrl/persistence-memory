"""Process-local, tenant-bound session windows for chat turns.

This store is intentionally not durable and intentionally not shared:
a restart drops it, two replicas never share it, and it accepts no
caller-supplied history. On restart or eviction the window starts empty,
the turn's receipt is marked ``context_reset=True``, and turn numbering
continues from the transactional receipt-store head — receipt continuity
is independent of this ephemeral context.
"""

from __future__ import annotations

import threading
import time

from .models import Turn


class SessionStore:
    """Bounded in-memory turn windows keyed by ``(tenant_key, session_id)``."""

    def __init__(self, max_sessions: int = 256, max_turns: int = 60, ttl_s: float = 1800.0) -> None:
        self.max_sessions = max_sessions
        self.max_turns = max_turns
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], tuple[float, list[Turn]]] = {}

    def load(self, tenant_key: str, session_id: str) -> list[Turn]:
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            found = self._items.get((tenant_key, session_id))
            if found is None:
                return []
            _, turns = found
            self._items[(tenant_key, session_id)] = (now, turns)
            return [t.model_copy() for t in turns]

    def append(self, tenant_key: str, session_id: str, turns: list[Turn]) -> None:
        if not turns:
            return
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            key = (tenant_key, session_id)
            existing = self._items.get(key)
            history = list(existing[1]) if existing else []
            history.extend(t.model_copy() for t in turns)
            if len(history) > self.max_turns:
                history = history[-self.max_turns :]
            self._items[key] = (now, history)
            self._trim_sessions()

    def has_window(self, tenant_key: str, session_id: str) -> bool:
        """True when a live window exists — distinguishes reset from first contact."""
        with self._lock:
            return (tenant_key, session_id) in self._items

    def reset(self, tenant_key: str, session_id: str) -> None:
        with self._lock:
            self._items.pop((tenant_key, session_id), None)

    def _evict(self, now: float) -> None:
        expired = [key for key, (seen, _) in self._items.items() if now - seen > self.ttl_s]
        for key in expired:
            del self._items[key]

    def _trim_sessions(self) -> None:
        overflow = len(self._items) - self.max_sessions
        if overflow <= 0:
            return
        oldest = sorted(self._items.items(), key=lambda item: item[1][0])[:overflow]
        for key, _ in oldest:
            del self._items[key]
