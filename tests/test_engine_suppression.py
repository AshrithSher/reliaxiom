"""TDD: detection ignores anomalies caused by the agent's own actions (invariant #5). When
the agent restarts a service, that service's brief silence/restart must NOT become a new
incident — the change log tags the action and the engine suppresses anomalies for that
service within the suppression window."""
from datetime import timedelta

from sre_agent.changelog import ChangeLog, ChangeLogEntry
from sre_agent.detect.engine import DetectionEngine
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, fast_config
from tests.test_engine import ScriptedDetector


def agent_restart(changelog, service, at_s):
    changelog.record(ChangeLogEntry(ts=T0 + timedelta(seconds=at_s), actor="sre-agent",
                                    service=service, change_type="restart_container",
                                    detail="restart"))


def make(changelog):
    cfg = fast_config()  # debounce 20s, suppression default 120s
    det = ScriptedDetector()
    return DetectionEngine([det], cfg, changelog=changelog), det


def tick(engine, window, s):
    return engine.tick(window, T0 + timedelta(seconds=s))


def test_anomaly_from_agent_restart_is_suppressed(tmp_path):
    cl = ChangeLog(tmp_path / "c.db")
    agent_restart(cl, "api", at_s=5)        # the agent just restarted api
    engine, det = make(cl)
    window = SlidingWindow()
    det.active = True                        # api looks anomalous right after the restart
    tick(engine, window, 0)
    assert tick(engine, window, 25) == []    # debounce elapsed, but suppressed → no candidate


def test_anomaly_fires_without_agent_action(tmp_path):
    cl = ChangeLog(tmp_path / "c.db")        # empty — no agent action
    engine, det = make(cl)
    window = SlidingWindow()
    det.active = True
    tick(engine, window, 0)
    assert len(tick(engine, window, 25)) == 1   # genuine anomaly fires normally


def test_suppression_expires(tmp_path):
    cl = ChangeLog(tmp_path / "c.db")
    agent_restart(cl, "api", at_s=0)
    cfg = fast_config()
    cfg.action_suppression_s = 30.0          # short suppression for the test
    det = ScriptedDetector()
    engine = DetectionEngine([det], cfg, changelog=cl)
    window = SlidingWindow()
    det.active = True
    tick(engine, window, 0)
    assert tick(engine, window, 25) == []     # still within suppression (track forgotten)
    # past suppression the track restarts and must re-accumulate debounce, then fires
    assert tick(engine, window, 200) == []
    assert len(tick(engine, window, 225)) == 1


def test_only_acted_service_is_suppressed(tmp_path):
    cl = ChangeLog(tmp_path / "c.db")
    agent_restart(cl, "worker", at_s=5)       # agent restarted worker, not api
    engine, det = make(cl)                    # det fires for api
    window = SlidingWindow()
    det.active = True
    tick(engine, window, 0)
    assert len(tick(engine, window, 25)) == 1  # api anomaly is unrelated → still fires
