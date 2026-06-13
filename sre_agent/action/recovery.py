"""Per-fingerprint recovery predicates (D-005). Verification asks a concrete question per
fault type — not 'does it look normal' — so the agent never auto-resolves a partial
recovery (e.g. a queue that's draining but still deep)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sre_agent.config import Config
from sre_agent.incident.models import Incident
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import SignalSnapshot


class RecoveryEvaluator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def recovered(self, incident: Incident, window: SlidingWindow,
                  signals: SignalSnapshot | None, now: datetime) -> bool:
        fault = incident.fault_type
        if fault == "silence":
            return self._logging_again(incident, window, now)
        if fault == "queue_growth":
            return self._queue_ok(signals) and self._logging_again(incident, window, now)
        if fault in ("unreachable",) or "unreachable" in fault:
            return self._errors_cleared(incident, window, now) \
                and self._logging_again(incident, window, now)
        # error-rate style faults (internal_error, error_rate, ...) and the default
        return self._errors_cleared(incident, window, now)

    # --- conditions --------------------------------------------------------------
    def _logging_again(self, incident: Incident, window: SlidingWindow, now: datetime) -> bool:
        """Every affected service that is expected to log steadily is logging again."""
        for svc in incident.services:
            if svc not in self.cfg.silence_services:
                continue
            last = window.last_seen(svc)
            if last is None or (now - last).total_seconds() > self.cfg.silence_threshold_s:
                return False
        return True

    def _errors_cleared(self, incident: Incident, window: SlidingWindow, now: datetime) -> bool:
        since = now - timedelta(seconds=self.cfg.recovery_check_s)
        return all(len(window.error_records(svc, since)) <= self.cfg.recovery_error_tolerance
                   for svc in incident.services)

    def _queue_ok(self, signals: SignalSnapshot | None) -> bool:
        if signals is None or signals.redis_queue_depth is None:
            return True   # no signal to contradict recovery
        return signals.redis_queue_depth < self.cfg.queue_depth_threshold
