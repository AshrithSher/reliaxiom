"""Per-fingerprint recovery predicates (D-005). Verification asks a concrete question per
fault type — not 'does it look normal' — so the agent never auto-resolves a partial
recovery (e.g. a queue that's draining but still deep).

Metric-detected faults are verified on the **same metric** they were detected on (D-035): a
`metric_latency_p95` incident is only 'recovered' when the Prometheus p95 is back under
threshold, not merely when error logs went quiet. The metric source is optional and degrades
to the log-based checks when it is absent or unreachable — a momentarily-down backend can
never wedge an incident open, but it also can never declare a premature victory."""
from __future__ import annotations

from datetime import datetime, timedelta

from sre_agent.config import Config
from sre_agent.incident.models import Incident
from sre_agent.models import SignalSnapshot
from sre_agent.telemetry.promql import (error_ratio_expr, latency_p95_expr,
                                        queue_saturation_expr)
from sre_agent.telemetry.sources import LogSource, MetricSource


class RecoveryEvaluator:
    def __init__(self, cfg: Config, metrics: MetricSource | None = None) -> None:
        self.cfg = cfg
        self.metrics = metrics

    def recovered(self, incident: Incident, logs: LogSource,
                  signals: SignalSnapshot | None, now: datetime) -> bool:
        fault = incident.fault_type
        # metric-detected faults: confirm on the metric first, with a log sanity check.
        if fault == "metric_latency_p95":
            return self._latency_ok(incident, now) and self._errors_cleared(incident, logs, now)
        if fault == "metric_error_ratio":
            return self._error_ratio_ok(incident, now) and self._errors_cleared(incident, logs, now)
        if fault == "metric_saturation":
            return self._saturation_ok(incident, signals, now)
        # log/trace faults
        if fault == "silence":
            return self._logging_again(incident, logs, now)
        if fault == "queue_growth":
            return self._queue_ok(signals) and self._logging_again(incident, logs, now)
        if fault in ("unreachable",) or "unreachable" in fault:
            return self._root_container_up(incident, signals) \
                and self._errors_cleared(incident, logs, now) \
                and self._logging_again(incident, logs, now)
        # error-rate style faults (internal_error, error_rate, trace_error_rate, default)
        return self._errors_cleared(incident, logs, now)

    # --- log/signal conditions ---------------------------------------------------
    def _logging_again(self, incident: Incident, logs: LogSource, now: datetime) -> bool:
        """Every affected service that is expected to log steadily is logging again."""
        for svc in incident.services:
            if svc not in self.cfg.silence_services:
                continue
            last = logs.last_seen(svc)
            if last is None or (now - last).total_seconds() > self.cfg.silence_threshold_s:
                return False
        return True

    def _root_container_up(self, incident: Incident, signals: SignalSnapshot | None) -> bool:
        """For an unreachable dependency, the authoritative recovery signal is the container
        itself being back up. When a container-down candidate is the *only* evidence (e.g. a db
        outage behind a broken auth tier produced no downstream error logs), the log-based
        checks below are vacuously true while the dependency is still gone — so without this the
        agent would declare a premature victory. Degrades to True when signals don't track the
        container (never wedges an incident open on a missing poll)."""
        if signals is None:
            return True
        cs = signals.containers.get(incident.root_service)
        if cs is None:
            return True
        return cs.status == "running"

    def _errors_cleared(self, incident: Incident, logs: LogSource, now: datetime) -> bool:
        since = now - timedelta(seconds=self.cfg.recovery_check_s)
        return all(len(logs.error_records(svc, since)) <= self.cfg.recovery_error_tolerance
                   for svc in incident.services)

    def _queue_ok(self, signals: SignalSnapshot | None) -> bool:
        if signals is None or signals.redis_queue_depth is None:
            return True   # no signal to contradict recovery
        return signals.redis_queue_depth < self.cfg.queue_depth_threshold

    # --- metric conditions (degrade to True when the metric is unavailable) -------
    def _metric_services(self, incident: Incident) -> list[str]:
        return [s for s in incident.services if s in self.cfg.metric_services]

    def _latency_ok(self, incident: Incident, now: datetime) -> bool:
        if self.metrics is None:
            return True
        for svc in self._metric_services(incident):
            p95 = self.metrics.instant(latency_p95_expr(svc), now)
            if p95 is not None and p95 >= self.cfg.metric_latency_p95_threshold_ms:
                return False   # still slow → not recovered
        return True

    def _error_ratio_ok(self, incident: Incident, now: datetime) -> bool:
        if self.metrics is None:
            return True
        for svc in self._metric_services(incident):
            ratio = self.metrics.instant(error_ratio_expr(svc), now)
            if ratio is not None and ratio >= self.cfg.metric_error_ratio_threshold:
                return False   # still erroring → not recovered
        return True

    def _saturation_ok(self, incident: Incident, signals: SignalSnapshot | None,
                       now: datetime) -> bool:
        if self.metrics is not None:
            depth = self.metrics.instant(queue_saturation_expr(), now)
            if depth is not None:
                return depth < self.cfg.queue_depth_threshold
        return self._queue_ok(signals)   # fall back to the polled signal
