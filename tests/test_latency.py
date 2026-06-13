"""TDD red phase: latency p95 detector (M1).

Watches loadgen's reported latency_ms — the user-facing signal — and fires when the
p95 over the detection window breaches the threshold. p95, not average: a few slow
requests must not hide behind many fast ones, and one outlier must not page anyone.
"""
from datetime import timedelta

from sre_agent.detect.latency import LatencyDetector
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, fast_config, rec


def cfg():
    c = fast_config()
    c.latency_p95_threshold_ms = 1000.0
    c.latency_min_samples = 10
    return c


def fill_latency(window, values_ms, service="loadgen", start_s=0.0, every_s=1.0):
    for i, value in enumerate(values_ms):
        window.append(rec(service, start_s + i * every_s, latency_ms=value))


def test_fires_when_p95_breaches():
    window = SlidingWindow()
    # 20 samples: 18 fast, 2 at 5s — p95 = 5000ms > 1000ms threshold
    fill_latency(window, [100.0] * 18 + [5000.0, 5000.0])
    anomalies = LatencyDetector(cfg()).check(window, T0 + timedelta(seconds=30))
    assert [a.service for a in anomalies] == ["loadgen"]
    assert "p95" in anomalies[0].detail


def test_quiet_when_p95_healthy_despite_one_outlier():
    window = SlidingWindow()
    # one 8s outlier among 39 fast requests: p95 stays ~100ms, must NOT fire
    fill_latency(window, [100.0] * 39 + [8000.0])
    assert LatencyDetector(cfg()).check(window, T0 + timedelta(seconds=50)) == []


def test_quiet_when_average_hides_slowness_is_not_fooled():
    window = SlidingWindow()
    # every request at 1.5s: average and p95 both bad — must fire
    fill_latency(window, [1500.0] * 20)
    assert len(LatencyDetector(cfg()).check(window, T0 + timedelta(seconds=30))) == 1


def test_quiet_below_min_samples():
    window = SlidingWindow()
    # 3 slow requests is not a trend — too few samples to judge
    fill_latency(window, [5000.0] * 3)
    assert LatencyDetector(cfg()).check(window, T0 + timedelta(seconds=10)) == []


def test_only_watches_latency_services():
    window = SlidingWindow()
    # worker job latencies are background lag, not user-facing latency
    fill_latency(window, [5000.0] * 20, service="worker")
    assert LatencyDetector(cfg()).check(window, T0 + timedelta(seconds=30)) == []


def test_ignores_records_outside_window():
    window = SlidingWindow()
    fill_latency(window, [5000.0] * 20)  # old spike at t=0..20
    now = T0 + timedelta(seconds=fast_config().error_rate_window_s + 120)
    assert LatencyDetector(cfg()).check(window, now) == []
