"""TDD: queue-depth growth detector. The worker heartbeats its Redis queue depth. A
backlog that is both above threshold AND still climbing means the worker is falling
behind (slow consumer, redis backed up). A draining or flat queue is not an incident —
and a queue that has simply stopped being reported (worker dead) is the silence
detector's job, not this one."""
from datetime import timedelta

from sre_agent.detect.queue_depth import QueueDepthDetector
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, fast_config, rec


def cfg():
    c = fast_config()
    c.queue_depth_window_s = 120.0
    c.queue_depth_threshold = 50
    c.queue_depth_min_samples = 2
    return c


def heartbeats(window, depths, start_s=0.0, every_s=15.0, service="worker"):
    for i, d in enumerate(depths):
        window.append(rec(service, start_s + i * every_s, event="heartbeat", queue_depth=d))


def now_at(s):
    return T0 + timedelta(seconds=s)


def test_fires_when_above_threshold_and_growing():
    window = SlidingWindow()
    heartbeats(window, [10, 30, 60, 90])  # climbing through threshold
    anomalies = QueueDepthDetector(cfg()).check(window, now_at(60))
    assert [a.service for a in anomalies] == ["worker"]
    assert "90" in anomalies[0].detail


def test_quiet_when_draining():
    window = SlidingWindow()
    heartbeats(window, [90, 70, 50, 20])  # backlog clearing
    assert QueueDepthDetector(cfg()).check(window, now_at(60)) == []


def test_quiet_when_growing_but_below_threshold():
    window = SlidingWindow()
    heartbeats(window, [5, 10, 20, 30])  # rising but not yet a problem
    assert QueueDepthDetector(cfg()).check(window, now_at(60)) == []


def test_quiet_when_flat_high():
    window = SlidingWindow()
    heartbeats(window, [60, 60, 60])  # high but not growing — capacity, not incident
    assert QueueDepthDetector(cfg()).check(window, now_at(45)) == []


def test_quiet_with_single_sample():
    window = SlidingWindow()
    heartbeats(window, [90])  # can't measure growth from one point
    assert QueueDepthDetector(cfg()).check(window, now_at(20)) == []


def test_only_samples_within_window():
    window = SlidingWindow()
    heartbeats(window, [10, 90], start_s=0.0)  # old samples at t=0 and t=15
    # window starts at now-120 = 480-120 = 360; both samples excluded
    assert QueueDepthDetector(cfg()).check(window, now_at(480)) == []
