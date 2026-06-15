from __future__ import annotations

from datetime import datetime, timedelta

from sre_agent.config import Config
from sre_agent.detect.base import Detector
from sre_agent.ingest.parser import LineParser
from sre_agent.models import Anomaly
from sre_agent.telemetry.sources import LogSource


class MalformedSpikeDetector(Detector):
    """Fire when corrupt-JSON lines exceed threshold within the window. Reads the parser's
    corrupt-line tally rather than the window — malformed lines never become LogRecords,
    so they can't live in the window. Plain-text dependency logs are excluded upstream
    (see LineParser), so a healthy postgres/redis cannot trip this."""

    signal_type = "malformed_spike"

    def __init__(self, cfg: Config, parser: LineParser) -> None:
        self.parser = parser
        self.window_s = cfg.malformed_window_s
        self.threshold = cfg.malformed_threshold

    def check(self, logs: LogSource, now: datetime) -> list[Anomaly]:
        since = now - timedelta(seconds=self.window_s)
        count = self.parser.corrupt_since(since)
        if count >= self.threshold:
            return [Anomaly(
                service="_ingest",
                signal_type=self.signal_type,
                detail=(f"{count} corrupt JSON lines in {self.window_s:.0f}s "
                        f"(threshold {self.threshold}) — garbage in the stream"),
                evidence=[f"{count} unparseable structured lines since {since.isoformat()}"],
            )]
        return []
