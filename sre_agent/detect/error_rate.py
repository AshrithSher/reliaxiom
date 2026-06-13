from __future__ import annotations

from datetime import datetime, timedelta

from sre_agent.config import Config
from sre_agent.detect.base import Detector
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import Anomaly


class ErrorRateDetector(Detector):
    """ERROR lines per service per window vs. static threshold (DECISIONS.md D-010)."""

    signal_type = "error_rate"

    def __init__(self, cfg: Config) -> None:
        self.window_s = cfg.error_rate_window_s
        self.threshold = cfg.error_rate_threshold

    def check(self, window: SlidingWindow, now: datetime) -> list[Anomaly]:
        since = now - timedelta(seconds=self.window_s)
        anomalies = []
        for service in window.services():
            errors = window.error_records(service, since)
            if len(errors) >= self.threshold:
                codes = _distinct_error_codes(errors)
                anomalies.append(Anomaly(
                    service=service,
                    signal_type=self.signal_type,
                    detail=(f"{len(errors)} error lines in last {self.window_s:.0f}s "
                            f"(threshold {self.threshold})"),
                    evidence=[str(r.raw) for r in errors[-5:]],
                    error_codes=codes,
                ))
        return anomalies


def _distinct_error_codes(errors) -> list[str]:
    """Stable-ordered distinct `error` field values from the records' raw payloads."""
    seen: dict[str, None] = {}
    for r in errors:
        code = r.raw.get("error")
        if isinstance(code, str):
            seen.setdefault(code, None)
    return list(seen)
