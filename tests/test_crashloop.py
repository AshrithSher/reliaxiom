"""TDD: crash-loop detector. A service that emits `startup` repeatedly is restarting in
a loop — the memleak scenario's tail (OOM kill → docker restart → startup, again and
again). One startup is a normal boot; several in a short window is a crash-loop."""
from datetime import timedelta

from sre_agent.detect.crashloop import CrashLoopDetector
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, fast_config, rec


def cfg():
    c = fast_config()
    c.crashloop_window_s = 120.0
    c.crashloop_threshold = 3
    return c


def now_at(s):
    return T0 + timedelta(seconds=s)


def test_fires_on_repeated_startups():
    window = SlidingWindow()
    for t in (10, 40, 70):  # 3 startups in 120s window
        window.append(rec("api", t, event="startup"))
    anomalies = CrashLoopDetector(cfg()).check(window, now_at(80))
    assert [a.service for a in anomalies] == ["api"]
    assert "3" in anomalies[0].detail


def test_single_startup_is_normal_boot():
    window = SlidingWindow()
    window.append(rec("api", 10, event="startup"))
    assert CrashLoopDetector(cfg()).check(window, now_at(80)) == []


def test_old_startups_outside_window_dont_count():
    window = SlidingWindow()
    for t in (1, 2, 3):  # all far in the past
        window.append(rec("worker", t, event="startup"))
    assert CrashLoopDetector(cfg()).check(window, now_at(500)) == []


def test_counts_per_service():
    window = SlidingWindow()
    for t in (10, 40, 70):
        window.append(rec("api", t, event="startup"))
    window.append(rec("worker", 50, event="startup"))  # worker booted once, fine
    anomalies = CrashLoopDetector(cfg()).check(window, now_at(80))
    assert [a.service for a in anomalies] == ["api"]


def test_non_startup_events_ignored():
    window = SlidingWindow()
    for t in (10, 40, 70):
        window.append(rec("api", t, event="request"))  # plenty of traffic, no restarts
    assert CrashLoopDetector(cfg()).check(window, now_at(80)) == []
