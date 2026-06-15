"""Self-monitoring metrics (Maturity 11) — the agent watches itself the way it watches the lab.

A tiny dependency-free Prometheus exposition (no prometheus_client needed): Counter / Gauge /
Histogram with label support and a `render()` that emits the standard text format on the health
server's `/metrics`. `AgentMetrics` is the semantic facade the rest of the agent calls — the
manager records lifecycle events (detection latency, MTTR, escalations, action success), the main
loop stamps the heartbeat and leader gauge. These are the self-SLO signals an operator alerts on
(deploy/alerts.yml): a stalled heartbeat (dead-man's switch), a spiking false-positive proxy, MTTR
or escalation-rate regressions — the same on-call path the agent uses for lab incidents.

The agent emitting Prometheus metrics also closes the loop philosophically: the watcher is now a
monitored production service like any other (VISION §6)."""
from __future__ import annotations

import threading
import time
from typing import Iterable

_LabelKey = tuple[tuple[str, str], ...]


def _key(labels: dict[str, str]) -> _LabelKey:
    return tuple(sorted(labels.items()))


def _fmt_labels(key: _LabelKey) -> str:
    if not key:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
    return "{" + inner + "}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Metric:
    def __init__(self, name: str, help: str, labelnames: Iterable[str] = ()) -> None:
        self.name = name
        self.help = help
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()


class Counter(_Metric):
    def __init__(self, name: str, help: str, labelnames: Iterable[str] = ()) -> None:
        super().__init__(name, help, labelnames)
        self._vals: dict[_LabelKey, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with self._lock:
            k = _key(labels)
            self._vals[k] = self._vals.get(k, 0.0) + amount

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for k, v in sorted(self._vals.items()):
            out.append(f"{self.name}{_fmt_labels(k)} {_num(v)}")
        if not self._vals:
            out.append(f"{self.name} 0")
        return out


class Gauge(_Metric):
    def __init__(self, name: str, help: str, labelnames: Iterable[str] = ()) -> None:
        super().__init__(name, help, labelnames)
        self._vals: dict[_LabelKey, float] = {}

    def set(self, value: float, **labels: str) -> None:
        with self._lock:
            self._vals[_key(labels)] = float(value)

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        for k, v in sorted(self._vals.items()):
            out.append(f"{self.name}{_fmt_labels(k)} {_num(v)}")
        if not self._vals:
            out.append(f"{self.name} 0")
        return out


_DEFAULT_BUCKETS = (1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)


class Histogram(_Metric):
    def __init__(self, name: str, help: str, buckets: Iterable[float] = _DEFAULT_BUCKETS,
                 labelnames: Iterable[str] = ()) -> None:
        super().__init__(name, help, labelnames)
        self._buckets = tuple(sorted(buckets))
        self._counts: dict[_LabelKey, list[int]] = {}
        self._sum: dict[_LabelKey, float] = {}
        self._n: dict[_LabelKey, int] = {}

    def observe(self, value: float, **labels: str) -> None:
        with self._lock:
            k = _key(labels)
            counts = self._counts.setdefault(k, [0] * len(self._buckets))
            for i, b in enumerate(self._buckets):
                if value <= b:
                    counts[i] += 1
            self._sum[k] = self._sum.get(k, 0.0) + value
            self._n[k] = self._n.get(k, 0) + 1

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for k in sorted(self._counts):
            counts, total = self._counts[k], self._n[k]
            base = dict(k)
            for i, b in enumerate(self._buckets):
                lk = _key({**base, "le": _num(b)})
                out.append(f"{self.name}_bucket{_fmt_labels(lk)} {counts[i]}")
            inf = _key({**base, "le": "+Inf"})
            out.append(f"{self.name}_bucket{_fmt_labels(inf)} {total}")
            out.append(f"{self.name}_sum{_fmt_labels(k)} {_num(self._sum[k])}")
            out.append(f"{self.name}_count{_fmt_labels(k)} {total}")
        return out


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


class AgentMetrics:
    """The agent's self-SLO signals. Semantic methods so callers never touch raw metrics."""

    def __init__(self) -> None:
        self.candidates = Counter("sre_candidates_total",
                                  "Confirmed incident candidates by signal type", ["signal_type"])
        self.incidents = Counter("sre_incidents_total",
                                 "Incidents created by severity", ["severity"])
        self.outcomes = Counter("sre_incident_outcomes_total",
                                "Incident terminal outcomes", ["outcome"])
        self.escalations = Counter("sre_escalations_total", "Incidents escalated to a human")
        self.actions = Counter("sre_actions_total",
                               "Remediation actions executed", ["action", "result"])
        self.stream_blind = Counter("sre_stream_blind_total",
                                    "Agent-blind (stream_blind) agent-health alerts")
        self.diagnoses = Counter("sre_diagnoses_total", "LLM diagnosis runs", ["result"])
        self.detection_latency = Histogram("sre_detection_latency_seconds",
                                           "Symptom first-seen to incident created")
        self.diagnosis_latency = Histogram("sre_diagnosis_latency_seconds",
                                           "LLM diagnosis wall-clock per incident",
                                           buckets=(0.5, 1, 2, 5, 10, 20, 30, 60))
        self.mttr = Histogram("sre_mttr_seconds", "Incident detect-to-resolve time")
        self.last_tick = Gauge("sre_last_tick_timestamp_seconds",
                               "Unix time of the last completed agent tick (dead-man's switch)")
        self.leader = Gauge("sre_leader", "1 if this replica is the acting leader, else 0")
        self.up = Gauge("sre_up", "Always 1 while the process serves metrics")
        self.up.set(1)
        self._metrics = [self.candidates, self.incidents, self.outcomes, self.escalations,
                         self.actions, self.stream_blind, self.diagnoses,
                         self.detection_latency, self.diagnosis_latency, self.mttr,
                         self.last_tick, self.leader, self.up]

    # --- semantic recorders ------------------------------------------------------
    def on_candidate(self, signal_type: str) -> None:
        self.candidates.inc(signal_type=signal_type)

    def on_incident_created(self, severity: str, detection_latency_s: float | None) -> None:
        self.incidents.inc(severity=severity)
        if detection_latency_s is not None and detection_latency_s >= 0:
            self.detection_latency.observe(detection_latency_s)

    def on_resolved(self, mttr_s: float | None) -> None:
        self.outcomes.inc(outcome="resolved")
        if mttr_s is not None and mttr_s >= 0:
            self.mttr.observe(mttr_s)

    def on_escalated(self) -> None:
        self.outcomes.inc(outcome="escalated")
        self.escalations.inc()

    def on_action(self, action_id: str, success: bool) -> None:
        self.actions.inc(action=action_id, result="success" if success else "failure")

    def on_diagnosis(self, latency_s: float, ok: bool) -> None:
        self.diagnoses.inc(result="ok" if ok else "failed")
        self.diagnosis_latency.observe(latency_s)

    def on_stream_blind(self) -> None:
        self.stream_blind.inc()

    def heartbeat(self, leader: bool) -> None:
        self.last_tick.set(time.time())
        self.leader.set(1 if leader else 0)

    def render(self) -> str:
        lines: list[str] = []
        for m in self._metrics:
            lines.extend(m.render())
        return "\n".join(lines) + "\n"


class NullMetrics:
    """No-op sink so the manager/main code calls the same API whether or not metrics are on."""

    def on_candidate(self, *a, **k) -> None: ...
    def on_incident_created(self, *a, **k) -> None: ...
    def on_resolved(self, *a, **k) -> None: ...
    def on_escalated(self, *a, **k) -> None: ...
    def on_action(self, *a, **k) -> None: ...
    def on_diagnosis(self, *a, **k) -> None: ...
    def on_stream_blind(self, *a, **k) -> None: ...
    def heartbeat(self, *a, **k) -> None: ...
    def render(self) -> str: return ""
