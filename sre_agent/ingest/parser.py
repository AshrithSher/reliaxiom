"""Tolerant JSON line parser. Malformed lines are counted, never fatal.

A spike in *corrupt JSON* — lines that tried to be JSON (`{`/`[`) and failed — is a signal
(a service emitting garbage, or a corrupted stream). Plain-text lines (postgres/redis log
in prose by design) are counted as malformed but are NOT corrupt JSON, so a healthy
dependency's chatter never feeds the spike detector."""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone

from sre_agent.models import LogRecord


class LineParser:
    def __init__(self, corrupt_retention_s: float = 900.0) -> None:
        self.parsed_count = 0
        self.malformed_count = 0
        self._corrupt_retention = timedelta(seconds=corrupt_retention_s)
        self._corrupt_ts: deque[datetime] = deque()

    def parse(self, line: str, now: datetime | None = None) -> LogRecord | None:
        line = line.strip()
        if not line:
            return None
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            record = LogRecord(
                ts=_parse_ts(data.get("ts")),
                service=str(data.get("service", "unknown")),
                level=str(data.get("level", "INFO")),
                event=data.get("event"),
                message=data.get("message"),
                request_id=data.get("request_id"),
                status=_to_int(data.get("status")),
                latency_ms=_to_float(data.get("latency_ms")),
                queue_depth=_to_int(data.get("queue_depth")),
                raw=data,
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            self.malformed_count += 1
            # only lines that *attempted* structured data are corrupt JSON; plain-text
            # postgres/redis lines are expected and excluded
            if line[0] in "{[":
                self._record_corrupt(now or datetime.now(timezone.utc))
            return None
        self.parsed_count += 1
        return record

    def _record_corrupt(self, now: datetime) -> None:
        self._corrupt_ts.append(now)
        cutoff = now - self._corrupt_retention
        while self._corrupt_ts and self._corrupt_ts[0] < cutoff:
            self._corrupt_ts.popleft()

    def corrupt_since(self, since: datetime) -> int:
        return sum(1 for ts in self._corrupt_ts if ts >= since)


def _parse_ts(value: object) -> datetime:
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # missing/bad timestamp: stamp with arrival time rather than dropping the line
    return datetime.now(timezone.utc)


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
