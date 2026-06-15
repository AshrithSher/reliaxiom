"""Debounce + cooldown around the detectors. The only producer of IncidentCandidates.

- Debounce: an anomaly must persist `debounce_s` before becoming a candidate.
- Grace: an anomaly may disappear for up to `grace_s` (one flaky tick) without
  resetting its debounce clock.
- Cooldown: after a candidate fires for a (service, signal) key, that key is muted
  for `cooldown_s` — one incident, one alert stream.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from typing import TYPE_CHECKING

from sre_agent.config import Config
from sre_agent.detect.base import Detector
from sre_agent.models import Anomaly, IncidentCandidate
from sre_agent.telemetry.sources import LogSource

if TYPE_CHECKING:
    from sre_agent.changelog import ChangeLog

_Key = tuple[str, str]  # (service, signal_type)


@dataclass
class _Track:
    first_seen: datetime
    last_seen: datetime
    latest: Anomaly


class DetectionEngine:
    def __init__(self, detectors: list[Detector], cfg: Config,
                 changelog: "ChangeLog | None" = None) -> None:
        self.detectors = detectors
        self.debounce = timedelta(seconds=cfg.debounce_s)
        self.grace = timedelta(seconds=cfg.grace_s)
        self.cooldown = timedelta(seconds=cfg.cooldown_s)
        self._changelog = changelog
        self._suppression = timedelta(seconds=cfg.action_suppression_s)
        self._tracks: dict[_Key, _Track] = {}
        self._muted_until: dict[_Key, datetime] = {}

    def _suppressed(self, service: str, now: datetime) -> bool:
        """True if the agent itself acted on this service recently — so the resulting
        anomaly is our own remediation, not a new fault (invariant #5)."""
        if self._changelog is None:
            return False
        recent = self._changelog.recent(now - self._suppression, now)
        return any(e.actor == "sre-agent" and e.service == service for e in recent)

    def tick(self, logs: LogSource, now: datetime) -> list[IncidentCandidate]:
        anomalies: list[Anomaly] = []
        for detector in self.detectors:
            anomalies.extend(detector.check(logs, now))

        seen: set[_Key] = set()
        for anomaly in anomalies:
            key = (anomaly.service, anomaly.signal_type)
            seen.add(key)
            track = self._tracks.get(key)
            if track is None:
                self._tracks[key] = _Track(first_seen=now, last_seen=now, latest=anomaly)
            else:
                track.last_seen = now
                track.latest = anomaly

        # drop tracks whose anomaly has been gone longer than the grace period
        for key in [k for k, t in self._tracks.items()
                    if k not in seen and now - t.last_seen > self.grace]:
            del self._tracks[key]

        candidates: list[IncidentCandidate] = []
        for key, track in list(self._tracks.items()):
            muted_until = self._muted_until.get(key)
            if muted_until is not None and now < muted_until:
                continue
            if now - track.first_seen >= self.debounce:
                if self._suppressed(track.latest.service, now):
                    del self._tracks[key]   # forget the agent-induced transient
                    continue
                candidates.append(IncidentCandidate(
                    services=[track.latest.service],
                    signal_type=track.latest.signal_type,
                    detail=track.latest.detail,
                    first_seen=track.first_seen,
                    confirmed_at=now,
                    evidence_sample=track.latest.evidence,
                    error_codes=track.latest.error_codes,
                    trace_ids=track.latest.trace_ids,
                ))
                self._muted_until[key] = now + self.cooldown
                del self._tracks[key]
        return candidates
