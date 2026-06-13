"""TDD: when the *whole* stream goes quiet at once, the agent is blind (dead tailer,
Docker hiccup, lab down) — it is not N independent service outages. The silence detector
must collapse a total blackout into a single `stream_blind` signal so the Incident
Manager raises one 'I can't see' alert instead of paging once per service.

Surfaced by the failed null test, which emitted gateway+webapp+api+worker silence
candidates simultaneously when the lab simply wasn't running.
"""
from datetime import timedelta

from sre_agent.detect.silence import SilenceDetector
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, fast_config, rec


def cfg():
    c = fast_config()
    c.silence_services = ["gateway", "webapp", "api", "worker"]
    return c


def quiet_now():
    return T0 + timedelta(seconds=100 + cfg().silence_threshold_s + 5)


def test_single_service_silence_is_per_service():
    """One service down among many healthy → ordinary per-service silence anomaly."""
    window = SlidingWindow()
    for s in cfg().silence_services:
        window.append(rec(s, 100))
    # everyone but worker keeps talking right up to now
    now = quiet_now()
    for s in ["gateway", "webapp", "api"]:
        window.append(rec(s, (now - T0).total_seconds() - 1))
    anomalies = SilenceDetector(cfg()).check(window, now)
    assert [a.service for a in anomalies] == ["worker"]
    assert anomalies[0].signal_type == "silence"


def test_total_blackout_collapses_to_one_blind_signal():
    """Every seen service silent at once → exactly one `stream_blind` anomaly, not N."""
    window = SlidingWindow()
    for s in cfg().silence_services:
        window.append(rec(s, 100))  # all last seen at t=100, then nothing
    anomalies = SilenceDetector(cfg()).check(window, quiet_now())
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.signal_type == "stream_blind"
    assert a.service != "gateway"  # not attributed to a single service
    # evidence should name the affected services so a human sees the scope
    assert "gateway" in a.detail and "worker" in a.detail


def test_blackout_only_counts_services_actually_seen():
    """A service never seen (e.g. not started) doesn't count toward 'all silent'."""
    window = SlidingWindow()
    # only api and worker have ever logged; gateway/webapp never appeared
    window.append(rec("api", 100))
    window.append(rec("worker", 100))
    anomalies = SilenceDetector(cfg()).check(window, quiet_now())
    # both seen services silent → still a blackout among what we can see
    assert len(anomalies) == 1
    assert anomalies[0].signal_type == "stream_blind"


def test_cold_start_stays_silent():
    """Nothing ever seen → no anomaly at all (can't be blind to a stream you never had)."""
    window = SlidingWindow()
    assert SilenceDetector(cfg()).check(window, quiet_now()) == []
