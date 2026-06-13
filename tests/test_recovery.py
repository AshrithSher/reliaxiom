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
