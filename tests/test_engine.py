from datetime import timedelta

from sre_agent.detect.base import Detector
from sre_agent.detect.engine import DetectionEngine
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import Anomaly
from tests.helpers import T0, fast_config


class ScriptedDetector(Detector):
    """Emits an anomaly whenever its `active` flag is on — lets tests drive time."""

    signal_type = "scripted"

    def __init__(self) -> None:
        self.active = False

    def check(self, window, now):
        if not self.active:
            return []
        return [Anomaly(service="api", signal_type=self.signal_type, detail="scripted",
                        evidence=["line"])]


def make_engine():
    cfg = fast_config()  # debounce 20s, grace 8s, cooldown 60s
    detector = ScriptedDetector()
    return DetectionEngine([detector], cfg), detector, SlidingWindow()


def tick_at(engine, window, seconds):
    return engine.tick(window, T0 + timedelta(seconds=seconds))


def test_debounce_blocks_short_blips():
    engine, det, window = make_engine()
    det.active = True
    assert tick_at(engine, window, 0) == []
    assert tick_at(engine, window, 10) == []
    det.active = False
    assert tick_at(engine, window, 15) == []
    # gone past grace: track must reset, nothing ever fires
    assert tick_at(engine, window, 30) == []
    det.active = True
    assert tick_at(engine, window, 35) == []  # fresh debounce clock


def test_persistent_anomaly_becomes_candidate():
    engine, det, window = make_engine()
    det.active = True
    assert tick_at(engine, window, 0) == []
    assert tick_at(engine, window, 10) == []
    candidates = tick_at(engine, window, 21)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.fingerprint() == "api:scripted"
    assert c.first_seen == T0
    assert c.evidence_sample == ["line"]


def test_grace_tolerates_one_flaky_tick():
    engine, det, window = make_engine()
    det.active = True
    tick_at(engine, window, 0)
    det.active = False
    tick_at(engine, window, 5)  # blip out, within 8s grace
    det.active = True
    tick_at(engine, window, 10)
    candidates = tick_at(engine, window, 21)
    assert len(candidates) == 1 and candidates[0].first_seen == T0


def test_cooldown_one_incident_one_alert():
    engine, det, window = make_engine()
    det.active = True
    tick_at(engine, window, 0)
    assert len(tick_at(engine, window, 21)) == 1
    # anomaly persists: no re-fire during 60s cooldown
    for t in range(25, 80, 5):
        assert tick_at(engine, window, t) == []
    # past cooldown and still broken: fires again (new alert stream)
    assert len(tick_at(engine, window, 105)) == 1
