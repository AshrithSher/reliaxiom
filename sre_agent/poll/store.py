"""Thread-safe store of the latest SignalSnapshot, and the poll cycle that fills it.

PollCycle.run_once runs every poller, merges their partial results into one snapshot, and
isolates failures: a poller that raises is skipped, never blanking the others."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from sre_agent.models import SignalSnapshot
from sre_agent.poll.pollers import Poller


class SignalStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: SignalSnapshot | None = None

    def set(self, snapshot: SignalSnapshot) -> None:
        with self._lock:
            self._latest = snapshot

    def latest(self) -> SignalSnapshot | None:
        with self._lock:
            return self._latest


class PollCycle:
    def __init__(self, pollers: list[Poller], store: SignalStore) -> None:
        self._pollers = pollers
        self._store = store

    def run_once(self, now: datetime) -> SignalSnapshot:
        merged: dict[str, Any] = {}
        for poller in self._pollers:
            try:
                merged.update(poller.poll())
            except Exception:
                continue  # one broken source must not sink the cycle
        snapshot = SignalSnapshot(ts=now, **merged)
        self._store.set(snapshot)
        return snapshot


class PollLoop:
    """Runs a PollCycle on a daemon thread every interval. Exceptions inside a cycle are
    already isolated per-poller by PollCycle; this loop additionally never dies on a
    cycle-level error."""

    def __init__(self, cycle: PollCycle, interval_s: float) -> None:
        self._cycle = cycle
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def run() -> None:
            while not self._stop.is_set():
                try:
                    self._cycle.run_once(datetime.now(timezone.utc))
                except Exception:
                    pass
                self._stop.wait(self._interval)

        self._thread = threading.Thread(target=run, daemon=True, name="poll-loop")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
