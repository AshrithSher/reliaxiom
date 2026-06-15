"""TDD: per-fingerprint recovery predicates (D-005). Recovery is a concrete, code-evaluated
condition — not 'looks normal' — so the agent never auto-resolves a partial recovery. Each
fault type defines what 'better' actually means."""
from datetime import timedelta

from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import ContainerState, LogRecord, SignalSnapshot
from tests.helpers import T0, fast_config, rec


def incident(fault_type, services, root=None):
    return Incident(id="INC-1", fingerprint=f"{root or services[0]}:{fault_type}", ticket_id="SRE-1",
                    state=IncidentState.VERIFYING, root_service=root or services[0],
                    services=services, fault_type=fault_type, severity="High",
                    first_seen=T0, updated_at=T0)


def cfg():
    c = fast_config()
    c.recovery_check_s = 60.0
    c.recovery_error_tolerance = 2
    c.silence_threshold_s = 30.0
    return c


def at(s):
    return T0 + timedelta(seconds=s)


# --- silence recovery ------------------------------------------------------------
def test_silence_not_recovered_while_quiet():
    w = SlidingWindow()
    w.append(rec("worker", 10))               # last logged at t=10
    ev = RecoveryEvaluator(cfg())
    assert not ev.recovered(incident("silence", ["worker"]), w, None, at(100))  # silent 90s


def test_silence_recovered_when_logging_resumes():
    w = SlidingWindow()
    w.append(rec("worker", 95))               # logging again right before now
    ev = RecoveryEvaluator(cfg())
    assert ev.recovered(incident("silence", ["worker"]), w, None, at(100))


# --- unreachable recovery (errors stopped AND service logging) -------------------
def test_unreachable_not_recovered_while_errors_persist():
    w = SlidingWindow()
    for i in range(5):
        w.append(rec("worker", 90 + i, level="ERROR", error="db_unreachable"))
    ev = RecoveryEvaluator(cfg())
    assert not ev.recovered(incident("unreachable", ["worker"], root="postgres"), w, None, at(100))


def test_unreachable_recovered_when_errors_clear_and_logging():
    w = SlidingWindow()
    for i in range(5):
        w.append(rec("worker", 10 + i, level="ERROR", error="db_unreachable"))  # old errors
    w.append(rec("worker", 98))               # healthy line now, no recent errors
    ev = RecoveryEvaluator(cfg())
    assert ev.recovered(incident("unreachable", ["worker"], root="postgres"), w, None, at(100))


def test_unreachable_not_recovered_while_root_container_still_down():
    # the container-down path: the incident's only service is the dependency itself, so the
    # log checks are vacuously clean. The live signal must veto a premature recovery.
    w = SlidingWindow()
    snap = SignalSnapshot(ts=at(100),
                          containers={"postgres": ContainerState(name="postgres", status="exited")})
    ev = RecoveryEvaluator(cfg())
    assert not ev.recovered(incident("unreachable", ["postgres"], root="postgres"), w, snap, at(100))


def test_unreachable_recovered_when_container_back_up_and_quiet():
    # operator (or the agent) restarts postgres → container running + downstream quiet → resolved
    w = SlidingWindow()
    w.append(rec("worker", 98))               # logging steadily again, no recent errors
    snap = SignalSnapshot(ts=at(100),
                          containers={"postgres": ContainerState(name="postgres", status="running")})
    ev = RecoveryEvaluator(cfg())
    assert ev.recovered(incident("unreachable", ["worker"], root="postgres"), w, snap, at(100))


# --- error-rate recovery ---------------------------------------------------------
def test_error_rate_recovered_below_tolerance():
    w = SlidingWindow()
    w.append(rec("api", 98, level="ERROR", status=500))   # one stray error within tolerance(2)
    w.append(rec("api", 99))
    ev = RecoveryEvaluator(cfg())
    assert ev.recovered(incident("internal_error", ["api"]), w, None, at(100))


def test_error_rate_not_recovered_above_tolerance():
    w = SlidingWindow()
    for i in range(5):
        w.append(rec("api", 95 + i, level="ERROR", status=500))
    ev = RecoveryEvaluator(cfg())
    assert not ev.recovered(incident("internal_error", ["api"]), w, None, at(100))


# --- queue-growth recovery uses the live signal ---------------------------------
def test_queue_growth_recovered_when_depth_low():
    w = SlidingWindow()
    w.append(rec("worker", 98, event="heartbeat", queue_depth=3))
    snap = SignalSnapshot(ts=at(100), redis_queue_depth=3,
                          containers={"worker": ContainerState(name="worker", status="running")})
    ev = RecoveryEvaluator(cfg())
    assert ev.recovered(incident("queue_growth", ["worker"]), w, snap, at(100))


def test_queue_growth_not_recovered_when_backlog_high():
    w = SlidingWindow()
    w.append(rec("worker", 98, event="heartbeat", queue_depth=500))
    snap = SignalSnapshot(ts=at(100), redis_queue_depth=500)
    ev = RecoveryEvaluator(cfg())
    assert not ev.recovered(incident("queue_growth", ["worker"]), w, snap, at(100))


# --- metric-aware recovery (D-035): verify on the same metric we detected on -------
class StubMetrics:
    """instant() returns a value for the first matching expr substring, else None."""

    def __init__(self, by_substr):
        self.by_substr = by_substr

    def instant(self, expr, at):
        for substr, value in self.by_substr.items():
            if substr in expr:
                return value
        return None

    def range(self, expr, start, end, step_s):
        return []


def _healthy_window():
    w = SlidingWindow()
    w.append(rec("api", 99))   # recent healthy line, no errors
    return w


def test_metric_latency_not_recovered_while_p95_high():
    # p95 still 2500ms (threshold 1000) → not recovered even though logs are clean
    ev = RecoveryEvaluator(cfg(), metrics=StubMetrics({"histogram_quantile": 2500.0}))
    assert not ev.recovered(incident("metric_latency_p95", ["api"]), _healthy_window(), None, at(100))


def test_metric_latency_recovered_when_p95_back_under_threshold():
    ev = RecoveryEvaluator(cfg(), metrics=StubMetrics({"histogram_quantile": 80.0}))
    assert ev.recovered(incident("metric_latency_p95", ["api"]), _healthy_window(), None, at(100))


def test_metric_error_ratio_not_recovered_while_ratio_high():
    ev = RecoveryEvaluator(cfg(), metrics=StubMetrics({'status=~"5.."': 0.6}))
    assert not ev.recovered(incident("metric_error_ratio", ["api"]), _healthy_window(), None, at(100))


def test_metric_error_ratio_recovered_when_ratio_low():
    ev = RecoveryEvaluator(cfg(), metrics=StubMetrics({'status=~"5.."': 0.0}))
    assert ev.recovered(incident("metric_error_ratio", ["api"]), _healthy_window(), None, at(100))


def test_metric_saturation_uses_metric_gauge():
    high = RecoveryEvaluator(cfg(), metrics=StubMetrics({"queue_depth": 500.0}))
    low = RecoveryEvaluator(cfg(), metrics=StubMetrics({"queue_depth": 3.0}))
    assert not high.recovered(incident("metric_saturation", ["worker"]), _healthy_window(), None, at(100))
    assert low.recovered(incident("metric_saturation", ["worker"]), _healthy_window(), None, at(100))


def test_metric_recovery_degrades_to_logs_when_metrics_absent():
    # no metric source: a metric fault falls back to the log-based checks, never wedges open
    ev = RecoveryEvaluator(cfg())   # metrics=None
    assert ev.recovered(incident("metric_latency_p95", ["api"]), _healthy_window(), None, at(100))
    # but if logs still show errors, it is correctly NOT recovered
    w = SlidingWindow()
    for i in range(5):
        w.append(rec("api", 95 + i, level="ERROR", status=500))
    assert not ev.recovered(incident("metric_error_ratio", ["api"]), w, None, at(100))
