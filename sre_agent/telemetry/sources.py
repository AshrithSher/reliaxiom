"""Telemetry-source SPIs — the seam that makes the engine substrate-agnostic (ROADMAP P0.1,
DECISIONS D-031). Detection reads through these interfaces, never a concrete backend.

Three read contracts, one per signal class:

- `LogSource` — the surface the detectors already used on `SlidingWindow`. `SlidingWindow`
  is the default in-memory implementation (fed by the tailer); a real backend (Loki) is a
  swap behind the same four methods.
- `MetricSource` — instant + range queries against a metrics backend (PromQL/Prometheus).
- `TraceSource` — per-service error rate + trace-by-id against a tracing backend (Tempo).

Implementations MUST follow the D-015 discipline: inject their IO, catch all transport/parse
failures, and return None / empty rather than raising or fabricating. A missing reading is
honest; a wrong one is a lie the diagnosis layer would later trust. Detection stays
deterministic — these sources feed code thresholds, never the LLM (invariant #1)."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from sre_agent.models import LogRecord, MetricPoint, Trace


@runtime_checkable
class LogSource(Protocol):
    """Recent parsed log lines, queryable per service. The detectors' read contract."""

    def services(self) -> list[str]: ...

    def last_seen(self, service: str) -> datetime | None: ...

    def records(self, service: str, since: datetime) -> list[LogRecord]: ...

    def error_records(self, service: str, since: datetime) -> list[LogRecord]: ...


@runtime_checkable
class MetricSource(Protocol):
    """A metrics backend queried with PromQL. Returns None on any failure (degrade, D-015)."""

    def instant(self, expr: str, at: datetime) -> float | None:
        """Single scalar from an instant query (e.g. a ratio or quantile right now)."""

    def range(
        self, expr: str, start: datetime, end: datetime, step_s: float
    ) -> list[MetricPoint]:
        """A series over a window. Empty list on failure."""


@runtime_checkable
class TraceSource(Protocol):
    """A tracing backend (Tempo/Jaeger). Returns None on any failure (degrade, D-015)."""

    def error_rate(self, service: str, since: datetime) -> float | None:
        """Fraction of error spans for `service` since `since`, or None if unavailable."""

    def trace(self, trace_id: str) -> Trace | None:
        """Fetch a single trace by id, or None."""
