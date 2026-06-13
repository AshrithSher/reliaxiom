from __future__ import annotations

import math
from datetime import datetime, timedelta

from sre_agent.config import Config
from sre_agent.detect.base import Detector
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import Anomaly


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. `pct` in [0, 1]. Assumes non-empty input."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct * len(ordered)))
    return ordered[rank - 1]


class LatencyDetector(Detector):
    """p95 latency on user-facing services vs. threshold. p95 (not average) so a slow
    tail can't hide behind a fast mean, and a single outlier can't page on its own.
    Requires a minimum sample count so a handful of slow requests isn't called a trend."""

    signal_type = "latency_p95"

    def __init__(self, cfg: Config) -> None:
        self.window_s = cfg.latency_window_s
        self.threshold_ms = cfg.latency_p95_threshold_ms
        self.min_samples = cfg.latency_min_samples
        self.services = cfg.latency_services

    def check(self, window: SlidingWindow, now: datetime) -> list[Anomaly]:
        since = now - timedelta(seconds=self.window_s)
        anomalies = []
        for service in self.services:
            latencies = [r.latency_ms for r in window.records(service, since)
                         if r.latency_ms is not None]
            if len(latencies) < self.min_samples:
                continue
            p95 = percentile(latencies, 0.95)
            if p95 >= self.threshold_ms:
                anomalies.append(Anomaly(
                    service=service,
                    signal_type=self.signal_type,
                    detail=(f"p95 latency {p95:.0f}ms over {len(latencies)} requests "
                            f"in {self.window_s:.0f}s (threshold {self.threshold_ms:.0f}ms)"),
                    evidence=[f"p95={p95:.0f}ms", f"n={len(latencies)}"],
                ))
        return anomalies
