from __future__ import annotations

from datetime import datetime, timedelta

from sre_agent.config import Config
from sre_agent.detect.base import Detector
from sre_agent.models import Anomaly
from sre_agent.telemetry.sources import LogSource


class CrashLoopDetector(Detector):
    """Repeated `startup` events from one service = a restart loop (e.g. memleak → OOM
    kill → docker restart → startup, over and over). One startup is a normal boot."""

    signal_type = "crash_loop"

    def __init__(self, cfg: Config) -> None:
        self.window_s = cfg.crashloop_window_s
        self.threshold = cfg.crashloop_threshold

    def check(self, logs: LogSource, now: datetime) -> list[Anomaly]:
        since = now - timedelta(seconds=self.window_s)
        anomalies = []
        for service in logs.services():
            starts = [r for r in logs.records(service, since) if r.event == "startup"]
            if len(starts) >= self.threshold:
                anomalies.append(Anomaly(
                    service=service,
                    signal_type=self.signal_type,
                    detail=(f"{len(starts)} startups in {self.window_s:.0f}s "
                            f"(threshold {self.threshold}) — restarting in a loop"),
                    evidence=[f"startup at {r.ts.isoformat()}" for r in starts[-5:]],
                ))
        return anomalies
