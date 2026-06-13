from datetime import timedelta

from sre_agent.detect.error_rate import ErrorRateDetector
from sre_agent.detect.silence import SilenceDetector
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, fast_config, fill_healthy, rec


def test_error_rate_fires_on_spike():
    cfg = fast_config()
    window = SlidingWindow()
    fill_healthy(window, ["api"], 60)
    for i in range(cfg.error_rate_threshold):
        window.append(rec("api", 50 + i, level="ERROR", status=500))
    anomalies = ErrorRateDetector(cfg).check(window, T0 + timedelta(seconds=60))
    assert [a.service for a in anomalies] == ["api"]
    assert anomalies[0].evidence  # carries sample lines


def test_error_rate_quiet_below_threshold():
    cfg = fast_config()
    window = SlidingWindow()
    fill_healthy(window, ["api", "webapp"], 60)
    window.append(rec("api", 55, level="ERROR", status=500))  # one stray error is fine
    assert ErrorRateDetector(cfg).check(window, T0 + timedelta(seconds=60)) == []


def test_error_rate_only_counts_recent_window():
    cfg = fast_config()
    window = SlidingWindow()
    for i in range(cfg.error_rate_threshold * 2):
        window.append(rec("api", i, level="ERROR"))  # old spike, t=0..10
    now = T0 + timedelta(seconds=cfg.error_rate_window_s + 30)
    assert ErrorRateDetector(cfg).check(window, now) == []


def test_silence_fires_when_chatty_service_goes_quiet():
    cfg = fast_config()
    window = SlidingWindow()
    fill_healthy(window, ["worker", "api"], 100)
    now = T0 + timedelta(seconds=100 + cfg.silence_threshold_s + 1)
    window.append(rec("api", 100 + cfg.silence_threshold_s, level="INFO"))  # api still talking
    anomalies = SilenceDetector(cfg).check(window, now)
    assert [a.service for a in anomalies] == ["worker"]


def test_silence_ignores_never_seen_services():
    cfg = fast_config()
    window = SlidingWindow()  # cold start: nothing seen yet
    assert SilenceDetector(cfg).check(window, T0) == []


def test_silence_ignores_quiet_by_design_services():
    cfg = fast_config()
    window = SlidingWindow()
    window.append(rec("postgres", 0))
    now = T0 + timedelta(seconds=300)
    assert SilenceDetector(cfg).check(window, now) == []  # postgres not in silence_services
