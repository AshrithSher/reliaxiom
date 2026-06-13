from __future__ import annotations

from datetime import datetime

from sre_agent.config import Config
from sre_agent.detect.base import Detector
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import Anomaly


class SilenceDetector(Detector):
    """A chatty service that stops logging is down or wedged (e.g. kill-worker, the
    'silent' fault). Only fires for services we have actually seen log at least once,
    so a cold agent start does not false-positive.

    A *total* blackout — every service we've ever seen going quiet at once — is not N
    independent outages; it means the agent itself is blind (dead tailer, Docker hiccup,
    lab down). That collapses into a single `stream_blind` signal so the Incident Manager
    raises one 'I can't see' alert, not one page per service."""

    signal_type = "silence"
    blind_signal_type = "stream_blind"

    def __init__(self, cfg: Config) -> None:
        self.services = cfg.silence_services
        self.threshold_s = cfg.silence_threshold_s

    def check(self, window: SlidingWindow, now: datetime) -> list[Anomaly]:
        seen = 0
        silent: list[tuple[str, datetime, float]] = []
        for service in self.services:
            last = window.last_seen(service)
            if last is None:
                continue  # never seen yet — can't distinguish silence from not-started
            seen += 1
            quiet_for = (now - last).total_seconds()
            if quiet_for >= self.threshold_s:
                silent.append((service, last, quiet_for))

        if not silent:
            return []

        # every service we can see has gone quiet → we're blind, not N services down
        if len(silent) == seen and seen > 1:
            names = ", ".join(s for s, _, _ in silent)
            worst = max(q for _, _, q in silent)
            return [Anomaly(
                service="_stream",
                signal_type=self.blind_signal_type,
                detail=(f"entire stream silent for {worst:.0f}s "
                        f"(threshold {self.threshold_s:.0f}s): {names} — agent is blind"),
                evidence=[f"{s} last line at {last.isoformat()}" for s, last, _ in silent],
            )]

        return [Anomaly(
            service=service,
            signal_type=self.signal_type,
            detail=f"no log lines for {quiet_for:.0f}s (threshold {self.threshold_s:.0f}s)",
            evidence=[f"last line at {last.isoformat()}"],
        ) for service, last, quiet_for in silent]
