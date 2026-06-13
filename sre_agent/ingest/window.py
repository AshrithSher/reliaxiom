"""Sliding-window store over the parsed stream. Thread-safe: the tailer thread appends,
the detection loop reads."""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta

from sre_agent.models import LogRecord


class SlidingWindow:
    def __init__(self, max_age_s: int = 900) -> None:
        self._max_age = timedelta(seconds=max_age_s)
        self._lock = threading.Lock()
        self._by_service: dict[str, deque[LogRecord]] = {}
        self._last_seen: dict[str, datetime] = {}

    def append(self, record: LogRecord) -> None:
        with self._lock:
            self._by_service.setdefault(record.service, deque()).append(record)
            prev = self._last_seen.get(record.service)
            if prev is None or record.ts > prev:
                self._last_seen[record.service] = record.ts

    def prune(self, now: datetime) -> None:
        cutoff = now - self._max_age
        with self._lock:
            for records in self._by_service.values():
                while records and records[0].ts < cutoff:
                    records.popleft()

    def services(self) -> list[str]:
        with self._lock:
            return list(self._by_service)

    def last_seen(self, service: str) -> datetime | None:
        with self._lock:
            return self._last_seen.get(service)

    def records(self, service: str, since: datetime) -> list[LogRecord]:
        with self._lock:
            records = self._by_service.get(service)
            if not records:
                return []
            return [r for r in records if r.ts >= since]

    def error_records(self, service: str, since: datetime) -> list[LogRecord]:
        return [r for r in self.records(service, since) if r.is_error]
