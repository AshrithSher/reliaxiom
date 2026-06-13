from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sre_agent.config import Config
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import LogRecord

T0 = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


def rec(service: str, offset_s: float, level: str = "INFO", **kwargs) -> LogRecord:
    return LogRecord(ts=T0 + timedelta(seconds=offset_s), service=service, level=level, **kwargs)


def fill_healthy(window: SlidingWindow, services: list[str], until_s: float,
                 every_s: float = 5.0) -> None:
    """Steady INFO chatter from every service from t=0 to until_s."""
    t = 0.0
    while t <= until_s:
        for service in services:
            window.append(rec(service, t))
        t += every_s


def fast_config() -> Config:
    """Short timings so tests don't simulate minutes of wall clock semantics by hand."""
    cfg = Config()
    cfg.debounce_s = 20.0
    cfg.grace_s = 8.0
    cfg.cooldown_s = 60.0
    cfg.silence_threshold_s = 15.0
    cfg.error_rate_threshold = 5
    return cfg
