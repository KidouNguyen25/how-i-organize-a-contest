"""In-process pub/sub used by the Server-Sent Events stream (`GET /api/v1/stream`).

Events are small change notifications (ids and counters only, never boards or secrets).
Each subscriber keeps only the latest event per (type, id) and receives them in short batches,
so a burst of moves costs one message per match per ~80 ms window.
"""

from __future__ import annotations

import threading
import time
from typing import Any

COALESCE_S = 0.08
MAX_CLIENTS = 300


class Subscriber:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._pending: dict[tuple[str, str | None], dict[str, Any]] = {}
        self.closed = False

    def put(self, event: dict[str, Any]) -> None:
        with self._cv:
            self._pending[(event["type"], event.get("id"))] = event  # latest wins
            self._cv.notify()

    def close(self) -> None:
        with self._cv:
            self.closed = True
            self._cv.notify()

    def get_batch(self, timeout: float) -> list[dict[str, Any]] | None:
        """Wait up to `timeout` for events; None means timeout (send a keep-alive)."""
        with self._cv:
            if not self._pending and not self.closed:
                self._cv.wait(timeout)
            if not self._pending:
                return [] if self.closed else None
        time.sleep(COALESCE_S)  # let a burst accumulate
        with self._cv:
            batch = list(self._pending.values())
            self._pending.clear()
        return batch


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: set[Subscriber] = set()
        self.published = 0

    def subscribe(self) -> Subscriber | None:
        with self._lock:
            if len(self._subs) >= MAX_CLIENTS:
                return None
            sub = Subscriber()
            self._subs.add(sub)
            return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        sub.close()
        with self._lock:
            self._subs.discard(sub)

    def publish(self, type_: str, id_: str | None = None, **data: Any) -> None:
        event = {"type": type_, "id": id_, **data}
        with self._lock:
            subs = list(self._subs)
            self.published += 1
        for sub in subs:
            sub.put(event)

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._subs)


HUB = Hub()
