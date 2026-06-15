"""TDD: the container-down detector (D-037). A stopped/unhealthy stateful dependency is a
fault in itself — detected from the polled SignalStore, not the log stream, so it tickets even
when the consumers log too slowly (redis) or not at all (a db behind a broken auth tier) to
trip the error-rate threshold. It emits the dependency error code so the correlator roots it on
the SAME `service:unreachable` fingerprint the log path produces (one fault = one ticket), and
it runs through the engine so it inherits debounce, cooldown, and action-suppression."""
from datetime import timedelta

from sre_agent.changelog import ChangeLog, ChangeLogEntry
from sre_agent.detect.container_down import ContainerDownDetector
from sre_agent.detect.engine import DetectionEngine
from sre_agent.incident.correlation import Correlator
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import ContainerState, SignalSnapshot
from sre_agent.poll.store import SignalStore
from tests.helpers import T0, fast_config


def _snap(at, **statuses):
    return SignalSnapshot(
        ts=at, containers={n: ContainerState(name=n, status=s) for n, s in statuses.items()})


def _store(at, **statuses):
    s = SignalStore()
    s.set(_snap(at, **statuses))
    return s


# --- the per-tick anomaly --------------------------------------------------------
def test_fires_for_down_dependency_with_its_error_code():
    det = ContainerDownDetector(fast_config(), _store(T0, postgres="exited", redis="running"))
    anomalies = det.check(SlidingWindow(), T0)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.service == "postgres"
    assert a.signal_type == "container_down"
    assert a.error_codes == ["db_unreachable"]   # roots the cascade on postgres


def test_fires_for_redis_the_slow_retry_consumer_case():
    det = ContainerDownDetector(fast_config(), _store(T0, postgres="running", redis="exited"))
    anomalies = det.check(SlidingWindow(), T0)
    assert [a.service for a in anomalies] == ["redis"]
    assert anomalies[0].error_codes == ["redis_unreachable"]


def test_silent_when_dependencies_running():
    det = ContainerDownDetector(fast_config(), _store(T0, postgres="running", redis="running"))
    assert det.check(SlidingWindow(), T0) == []


def test_silent_without_a_poll_reading():
    # never fabricate a down-state before the first poll lands
    det = ContainerDownDetector(fast_config(), SignalStore())
    assert det.check(SlidingWindow(), T0) == []


def test_ignores_untracked_containers():
    # a stateless service (api) going down is the silence detector's job, not this one's —
    # firing here too would double-ticket the same fault.
    det = ContainerDownDetector(fast_config(), _store(T0, postgres="running", api="exited"))
    assert det.check(SlidingWindow(), T0) == []


# --- through the engine: debounce, then a candidate that correlates correctly ----
def _tick_until_candidate(engine, cfg, horizon_s=60.0):
    w = SlidingWindow()
    t = 0.0
    while t <= horizon_s:
        cands = engine.tick(w, T0 + timedelta(seconds=t))
        if cands:
            return cands
        t += 2.0
    return []


def test_debounces_then_correlates_to_service_unreachable():
    cfg = fast_config()
    signals = _store(T0, postgres="exited", redis="running")
    engine = DetectionEngine([ContainerDownDetector(cfg, signals)], cfg)
    cands = _tick_until_candidate(engine, cfg)
    assert cands, "a persistently-down container must mature into a candidate"
    groups = Correlator(LAB_TOPOLOGY).correlate(cands)
    # same fingerprint the log-based db_unreachable path produces → they dedup into one incident
    assert any(g.fingerprint == "postgres:unreachable" for g in groups)


def test_does_not_fire_before_debounce_elapses():
    cfg = fast_config()
    signals = _store(T0, postgres="exited", redis="running")
    engine = DetectionEngine([ContainerDownDetector(cfg, signals)], cfg)
    # one tick well inside the debounce window must not yet produce a candidate
    assert engine.tick(SlidingWindow(), T0 + timedelta(seconds=cfg.debounce_s / 2)) == []


def test_suppressed_after_the_agent_restarts_the_container(tmp_path):
    # invariant #5: the brief down-state the agent's own restart causes must not alarm.
    cfg = fast_config()
    changelog = ChangeLog(tmp_path / "changes.db")
    changelog.record(ChangeLogEntry(ts=T0, actor="sre-agent", service="postgres",
                                    change_type="restart_container", detail="{'service': 'postgres'}"))
    signals = _store(T0, postgres="exited", redis="running")
    engine = DetectionEngine([ContainerDownDetector(cfg, signals)], cfg, changelog=changelog)
    # even across the full debounce window, the agent-tagged service is never ticketed
    cands = _tick_until_candidate(engine, cfg, horizon_s=cfg.action_suppression_s - 5)
    assert cands == []
