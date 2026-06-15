"""Cross-layer data objects. Everything that crosses a layer boundary is a pydantic model."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LogRecord(BaseModel):
    """One parsed line from the combined stream."""

    ts: datetime
    service: str
    level: str = "INFO"
    event: str | None = None
    message: str | None = None
    request_id: str | None = None
    status: int | None = None
    latency_ms: float | None = None
    queue_depth: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def is_error(self) -> bool:
        return self.level.upper() == "ERROR" or (self.status is not None and self.status >= 500)


class ContainerState(BaseModel):
    """One container's state from `docker inspect`."""

    name: str
    status: str            # running | exited | restarting | ...
    restart_count: int = 0

    @property
    def healthy(self) -> bool:
        return self.status == "running"


class SignalSnapshot(BaseModel):
    """Latest reading of the polled secondary signals — context alongside the log stream.
    Every field is optional: a poller that failed this cycle leaves its field as-is/None
    rather than fabricating data."""

    ts: datetime
    containers: dict[str, ContainerState] = Field(default_factory=dict)
    health: dict[str, bool] = Field(default_factory=dict)
    redis_queue_depth: int | None = None
    pg_connections: int | None = None


class MetricPoint(BaseModel):
    """One (timestamp, value) sample of a metric series."""

    ts: datetime
    value: float


class MetricSeries(BaseModel):
    """A labelled metric series — one PromQL result vector entry."""

    labels: dict[str, str] = Field(default_factory=dict)
    points: list[MetricPoint] = Field(default_factory=list)


class Span(BaseModel):
    """One span of a distributed trace (OTel/Tempo shape, trimmed to what we use)."""

    trace_id: str
    span_id: str
    parent_id: str | None = None
    service: str
    name: str
    start: datetime
    duration_ms: float
    status: str = "OK"  # OK | ERROR (OTel status_code, normalised)

    @property
    def is_error(self) -> bool:
        return self.status.upper() == "ERROR"


class Trace(BaseModel):
    """A whole trace — the cross-service story for one request."""

    trace_id: str
    spans: list[Span] = Field(default_factory=list)

    @property
    def services(self) -> list[str]:
        seen: dict[str, None] = {}
        for s in self.spans:
            seen.setdefault(s.service, None)
        return list(seen)


class Anomaly(BaseModel):
    """A single tick's observation that something is off. Pre-debounce."""

    service: str
    signal_type: str  # e.g. "error_rate", "silence"
    detail: str
    evidence: list[str] = Field(default_factory=list)  # sample raw log lines
    # dependency error codes seen in the evidence (e.g. "redis_unreachable") — lets the
    # correlator attribute a cascade to its upstream root
    error_codes: list[str] = Field(default_factory=list)


class IncidentCandidate(BaseModel):
    """A debounced, cooled-down anomaly — the detection layer's only output."""

    services: list[str]
    signal_type: str
    detail: str
    first_seen: datetime
    confirmed_at: datetime
    evidence_sample: list[str] = Field(default_factory=list)
    error_codes: list[str] = Field(default_factory=list)

    def fingerprint(self) -> str:
        return f"{'+'.join(sorted(self.services))}:{self.signal_type}"
