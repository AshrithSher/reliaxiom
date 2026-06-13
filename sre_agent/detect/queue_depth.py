from __future__ import annotations

from datetime import datetime, timedelta

from sre_agent.config import Config
from sre_agent.detect.base import Detector
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import Anomaly


class QueueDepthDetector(Detector):
    """Fire when a service's reported queue depth is both above threshold and still
    growing across the window — the consumer is falling behind. A draining or flat queue
    is not an incident; a queue that stopped being reported (dead worker) is the silence
    detector's job."""

    signal_type = "queue_growth"

    def __init__(self, cfg: Config) -> None:
        self.window_s = cfg.queue_depth_window_s
        self.threshold = cfg.queue_depth_threshold
        self.min_samples = cfg.queue_depth_min_samples

    def check(self, window: SlidingWindow, now: datetime) -> list[Anomaly]:
        since = now - timedelta(seconds=self.window_s)
        anomalies = []
        for service in window.services():
            samples = [r for r in window.records(service, since) if r.queue_depth is not None]
            if len(samples) < self.min_samples:
                continue
            first, last = samples[0].queue_depth, samples[-1].queue_depth
            assert first is not None and last is not None
            if last >= self.threshold and last > first:
                anomalies.append(Anomaly(
                    service=service,
                    signal_type=self.signal_type,
                    detail=(f"queue depth {first} → {last} over {self.window_s:.0f}s "
                            f"(threshold {self.threshold}) — consumer falling behind"),
                    evidence=[f"depth={s.queue_depth} at {s.ts.isoformat()}" for s in samples[-5:]],
                ))
        return anomalies
