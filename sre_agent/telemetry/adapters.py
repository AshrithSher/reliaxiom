"""Live telemetry adapters: Prometheus (metrics), Loki (logs), Tempo (traces).

Each implements one SPI from `sources.py` and follows the D-015 discipline used by the
pollers and the Jira store: the HTTP transport is **injected** (so request shaping and
response parsing are unit-tested offline against canned JSON), and every method **catches
all transport/parse failures and returns None / empty** rather than raising or fabricating.
A momentarily-unreachable backend must never crash detection or invent an anomaly.

All endpoints are local, unauthenticated HTTP (the free Grafana LGTM stack on localhost),
so the transport is GET-only with no auth header — mirror the Jira `Transport` seam if a
hosted, credentialed backend is swapped in later.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from sre_agent.ingest.parser import LineParser
from sre_agent.models import LogRecord, MetricPoint, Span, Trace

# transport(url) -> (status_code, response_text). Injected for testability.
Transport = Callable[[str], tuple[int, str]]


def _epoch(ts: datetime) -> float:
    return ts.timestamp()


def _http_get(timeout_s: float) -> Transport:
    def get(url: str) -> tuple[int, str]:
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    return get


class _JsonClient:
    """Shared GET-JSON helper. Returns the decoded body, or None on any failure."""

    def __init__(self, base_url: str, transport: Transport | None, timeout_s: float) -> None:
        self.base = base_url.rstrip("/")
        self._transport = transport or _http_get(timeout_s)

    def get(self, path: str, params: dict[str, str]) -> dict | None:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        try:
            status, text = self._transport(url)
            if status >= 400 or not text.strip():
                return None
            body = json.loads(text)
            return body if isinstance(body, dict) else None
        except Exception:  # noqa: BLE001 — degrade, never raise (D-015)
            return None


# --- Prometheus (MetricSource) ----------------------------------------------------
class PrometheusMetricSource:
    """Queries Prometheus with PromQL. `instant` returns a single scalar (the first result
    vector's value); `range` returns the first matrix series' points. Per-service detector
    expressions scope to one series, so taking the first result is the intended reading."""

    def __init__(self, base_url: str = "http://localhost:9090",
                 transport: Transport | None = None, timeout_s: float = 10.0) -> None:
        self._client = _JsonClient(base_url, transport, timeout_s)

    def instant(self, expr: str, at: datetime) -> float | None:
        body = self._client.get("/api/v1/query",
                                {"query": expr, "time": f"{_epoch(at):.3f}"})
        result = _prom_result(body)
        if not result:
            return None
        value = result[0].get("value")  # [ts, "stringValue"]
        return _to_float(value[1]) if isinstance(value, list) and len(value) == 2 else None

    def range(self, expr: str, start: datetime, end: datetime,
              step_s: float) -> list[MetricPoint]:
        body = self._client.get("/api/v1/query_range", {
            "query": expr, "start": f"{_epoch(start):.3f}",
            "end": f"{_epoch(end):.3f}", "step": f"{step_s:g}"})
        result = _prom_result(body)
        if not result:
            return []
        points: list[MetricPoint] = []
        for pair in result[0].get("values", []):  # [[ts, "val"], ...]
            if isinstance(pair, list) and len(pair) == 2:
                val = _to_float(pair[1])
                if val is not None:
                    points.append(MetricPoint(
                        ts=datetime.fromtimestamp(float(pair[0]), tz=timezone.utc), value=val))
        return points


def _prom_result(body: dict | None) -> list[dict]:
    if not body or body.get("status") != "success":
        return []
    data = body.get("data") or {}
    result = data.get("result")
    return result if isinstance(result, list) else []


# --- Loki (LogSource) -------------------------------------------------------------
class LokiLogSource:
    """A `LogSource` backed by Loki — the SlidingWindow's real-backend swap (D-031). Each
    Loki log line is the original JSON the service emitted, so we reuse the tolerant
    `LineParser`; a heterogeneous backend can pass its own `parse` to map foreign schemas
    onto `LogRecord`. Time-bounded queries use an injected `clock` (default: now, UTC)."""

    def __init__(self, base_url: str = "http://localhost:3100",
                 transport: Transport | None = None, timeout_s: float = 10.0,
                 service_label: str = "service",
                 parse: Callable[[str], LogRecord | None] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 lookback_s: float = 900.0, limit: int = 2000) -> None:
        self._client = _JsonClient(base_url, transport, timeout_s)
        self._label = service_label
        self._parse = parse or LineParser().parse
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lookback_s = lookback_s
        self._limit = limit

    def services(self) -> list[str]:
        body = self._client.get(f"/loki/api/v1/label/{self._label}/values", {})
        if not body or body.get("status") != "success":
            return []
        values = body.get("data")
        return [str(v) for v in values] if isinstance(values, list) else []

    def records(self, service: str, since: datetime) -> list[LogRecord]:
        end = self._clock()
        body = self._client.get("/loki/api/v1/query_range", {
            "query": '{%s="%s"}' % (self._label, service),
            "start": f"{int(_epoch(since) * 1e9)}",
            "end": f"{int(_epoch(end) * 1e9)}",
            "limit": str(self._limit), "direction": "forward"})
        records: list[LogRecord] = []
        for stream in _loki_streams(body):
            for entry in stream.get("values", []):  # [["<ns ts>", "<line>"], ...]
                if not (isinstance(entry, list) and len(entry) == 2):
                    continue
                record = self._parse(entry[1])
                if record is not None and record.ts >= since:
                    records.append(record)
        records.sort(key=lambda r: r.ts)
        return records

    def error_records(self, service: str, since: datetime) -> list[LogRecord]:
        return [r for r in self.records(service, since) if r.is_error]

    def last_seen(self, service: str) -> datetime | None:
        since = self._clock().fromtimestamp(
            _epoch(self._clock()) - self._lookback_s, tz=timezone.utc)
        records = self.records(service, since)
        return records[-1].ts if records else None


def _loki_streams(body: dict | None) -> list[dict]:
    if not body or body.get("status") != "success":
        return []
    data = body.get("data") or {}
    result = data.get("result")
    return result if isinstance(result, list) else []


# --- Tempo (TraceSource) ----------------------------------------------------------
class TempoTraceSource:
    """A `TraceSource` backed by Tempo. `error_rate` divides two TraceQL-metrics rate
    queries (error spans / total spans) for a service; `trace` fetches and parses an OTLP
    JSON trace by id. The exact Tempo wire shapes are validated at integration time (like
    the live poller glue, D-015); the parsing/arithmetic and the degrade-to-None paths are
    unit-tested with canned bodies."""

    def __init__(self, base_url: str = "http://localhost:3200",
                 transport: Transport | None = None, timeout_s: float = 10.0,
                 service_attr: str = "resource.service.name",
                 clock: Callable[[], datetime] | None = None,
                 window_s: float = 300.0) -> None:
        self._client = _JsonClient(base_url, transport, timeout_s)
        self._attr = service_attr
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._window_s = window_s

    def error_rate(self, service: str, since: datetime) -> float | None:
        end = self._clock()
        total = self._metric_latest(
            '{ %s = "%s" } | rate()' % (self._attr, service), since, end)
        errors = self._metric_latest(
            '{ %s = "%s" && status = error } | rate()' % (self._attr, service), since, end)
        if total is None or errors is None:
            return None
        if total <= 0:
            return 0.0
        return max(0.0, min(1.0, errors / total))

    def _metric_latest(self, traceql: str, start: datetime, end: datetime) -> float | None:
        body = self._client.get("/api/metrics/query_range", {
            "q": traceql, "start": f"{int(_epoch(start))}",
            "end": f"{int(_epoch(end))}", "step": f"{self._window_s:g}s"})
        result = _prom_result(body)  # Tempo metrics mirror the Prometheus result shape
        if not result:
            return None
        values = result[0].get("values")
        if isinstance(values, list) and values:
            last = values[-1]
            return _to_float(last[1]) if isinstance(last, list) and len(last) == 2 else None
        return None

    def trace(self, trace_id: str) -> Trace | None:
        body = self._client.get(f"/api/traces/{trace_id}", {})
        if not body:
            return None
        spans = _parse_otlp_spans(body)
        return Trace(trace_id=trace_id, spans=spans) if spans else None


def _parse_otlp_spans(body: dict) -> list[Span]:
    """Flatten an OTLP/JSON trace (Tempo `/api/traces/<id>`) into our Span list."""
    spans: list[Span] = []
    for batch in body.get("batches", []):
        service = _otlp_attr(batch.get("resource", {}).get("attributes", []),
                             "service.name") or "unknown"
        for scope in batch.get("scopeSpans", []) or batch.get("instrumentationLibrarySpans", []):
            for s in scope.get("spans", []):
                try:
                    start_ns = int(s.get("startTimeUnixNano", 0))
                    end_ns = int(s.get("endTimeUnixNano", start_ns))
                    status_code = (s.get("status") or {}).get("code", 0)
                    spans.append(Span(
                        trace_id=str(s.get("traceId", "")),
                        span_id=str(s.get("spanId", "")),
                        parent_id=str(s["parentSpanId"]) if s.get("parentSpanId") else None,
                        service=service,
                        name=str(s.get("name", "")),
                        start=datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc),
                        duration_ms=max(0.0, (end_ns - start_ns) / 1e6),
                        status="ERROR" if status_code == 2 else "OK"))
                except (TypeError, ValueError, KeyError):
                    continue  # skip a malformed span, keep the rest
    return spans


def _otlp_attr(attributes: list, key: str) -> str | None:
    for attr in attributes:
        if attr.get("key") == key:
            value = attr.get("value", {})
            return value.get("stringValue") or value.get("value")
    return None


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --- live wiring ------------------------------------------------------------------
def build_live_telemetry_sources(cfg) -> dict[str, object]:
    """Construct the enabled telemetry sources from config. Returns a dict with optional
    keys `metrics` (MetricSource) and `traces` (TraceSource); `logs` stays the in-memory
    SlidingWindow by default and is only swapped to Loki when `telemetry_logs == "loki"`."""
    sources: dict[str, object] = {}
    if getattr(cfg, "telemetry_metrics", "off") == "prometheus":
        sources["metrics"] = PrometheusMetricSource(cfg.prometheus_url)
    if getattr(cfg, "telemetry_traces", "off") == "tempo":
        sources["traces"] = TempoTraceSource(cfg.tempo_url)
    if getattr(cfg, "telemetry_logs", "tailed") == "loki":
        sources["logs"] = LokiLogSource(cfg.loki_url)
    return sources
