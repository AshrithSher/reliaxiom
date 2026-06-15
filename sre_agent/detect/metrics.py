"""Metric- and trace-based detectors (RED/USE). These query a `MetricSource`/`TraceSource`
held at construction (like `MalformedSpikeDetector` holds the parser) instead of the log
window — so production detection leans on a Prometheus histogram, not a log-derived guess.

They stay strictly deterministic (invariant #1): a fixed PromQL/TraceQL string and a code
threshold, no LLM. When the source returns None (backend down, no data) the detector emits
*nothing* — a missing metric is not an anomaly (D-015). They produce the same `Anomaly`
objects as the log detectors, so debounce/cooldown/correlation are unchanged."""
from __future__ import annotations

from datetime import datetime, timedelta

from sre_agent.config import Config
from sre_agent.detect.base import Detector
from sre_agent.models import Anomaly
from sre_agent.telemetry.promql import (error_ratio_expr, latency_p95_expr,
                                        queue_saturation_expr)
from sre_agent.telemetry.sources import LogSource, MetricSource, TraceSource


class MetricErrorRatioDetector(Detector):
    """RED-errors: the 5xx fraction per service from the request counter. Fires when the
    ratio is at/above threshold — the metric twin of the log `error_rate` detector, and the
    two collapse into one incident via correlation (invariant #3)."""

    signal_type = "metric_error_ratio"

    def __init__(self, cfg: Config, source: MetricSource) -> None:
        self.source = source
        self.services = cfg.metric_services
        self.threshold = cfg.metric_error_ratio_threshold

    def check(self, logs: LogSource, now: datetime) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        for service in self.services:
            expr = error_ratio_expr(service)
            ratio = self.source.instant(expr, now)
            if ratio is None:
                continue  # no data → no anomaly (degrade, never fabricate)
            if ratio >= self.threshold:
                anomalies.append(Anomaly(
                    service=service,
                    signal_type=self.signal_type,
                    detail=(f"5xx ratio {ratio:.0%} (threshold {self.threshold:.0%}) "
                            f"from request metrics"),
                    evidence=[f"http 5xx ratio={ratio:.3f}", f"promql={expr}"],
                ))
        return anomalies


class MetricLatencyDetector(Detector):
    """RED-duration: real p95 from the request-duration histogram. A histogram_quantile beats
    a log-derived p95 (no per-line scanning, no lag), so this fires earlier and cleaner than
    the log `latency_p95` detector on the same fault."""

    signal_type = "metric_latency_p95"

    def __init__(self, cfg: Config, source: MetricSource) -> None:
        self.source = source
        self.services = cfg.metric_services
        self.threshold_ms = cfg.metric_latency_p95_threshold_ms

    def check(self, logs: LogSource, now: datetime) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        for service in self.services:
            expr = latency_p95_expr(service)
            p95_ms = self.source.instant(expr, now)
            if p95_ms is None:
                continue
            if p95_ms >= self.threshold_ms:
                anomalies.append(Anomaly(
                    service=service,
                    signal_type=self.signal_type,
                    detail=(f"p95 latency {p95_ms:.0f}ms (threshold {self.threshold_ms:.0f}ms) "
                            f"from request histogram"),
                    evidence=[f"histogram p95={p95_ms:.0f}ms"],
                ))
        return anomalies


class SaturationDetector(Detector):
    """USE-saturation: the worker's job-queue backlog gauge exceeding threshold. Visible from
    metrics even when the worker is otherwise quiet — the metric twin of `queue_growth`."""

    signal_type = "metric_saturation"

    def __init__(self, cfg: Config, source: MetricSource) -> None:
        self.source = source
        self.threshold = cfg.queue_depth_threshold

    def check(self, logs: LogSource, now: datetime) -> list[Anomaly]:
        depth = self.source.instant(queue_saturation_expr(), now)
        if depth is None or depth < self.threshold:
            return []
        return [Anomaly(
            service="worker",
            signal_type=self.signal_type,
            detail=(f"job-queue depth {depth:.0f} (threshold {self.threshold}) — "
                    f"consumer saturated"),
            evidence=[f"queue_depth gauge={depth:.0f}"],
        )]


class TraceErrorDetector(Detector):
    """Per-service error-span rate from traces — a causality signal that feeds the future
    multi-signal correlation (P1.1). Fires when a service's error fraction over the window is
    at/above threshold."""

    signal_type = "trace_error_rate"

    def __init__(self, cfg: Config, source: TraceSource) -> None:
        self.source = source
        self.services = cfg.metric_services
        self.threshold = cfg.metric_error_ratio_threshold
        self.window_s = cfg.error_rate_window_s

    def check(self, logs: LogSource, now: datetime) -> list[Anomaly]:
        since = now - timedelta(seconds=self.window_s)
        anomalies: list[Anomaly] = []
        for service in self.services:
            rate = self.source.error_rate(service, since)
            if rate is None:
                continue
            if rate >= self.threshold:
                anomalies.append(Anomaly(
                    service=service,
                    signal_type=self.signal_type,
                    detail=(f"error-span rate {rate:.0%} (threshold {self.threshold:.0%}) "
                            f"from traces"),
                    evidence=[f"trace error rate={rate:.3f}"],
                ))
        return anomalies
