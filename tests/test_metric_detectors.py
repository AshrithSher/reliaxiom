"""TDD: the metric/trace detectors. Each is driven by a stub source returning fixed values,
so the threshold logic and the degrade-to-silence behaviour are tested without any backend."""
from datetime import datetime, timezone

from sre_agent.detect.metrics import (
    MetricErrorRatioDetector,
    MetricLatencyDetector,
    SaturationDetector,
    TraceErrorDetector,
)
from tests.helpers import fast_config

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


class StubMetrics:
    """Returns a value per substring matched in the PromQL expr; None if unmatched."""

    def __init__(self, by_substr):
        self.by_substr = by_substr

    def instant(self, expr, at):
        for substr, value in self.by_substr.items():
            if substr in expr:
                return value
        return None

    def range(self, expr, start, end, step_s):
        return []


class StubTraces:
    def __init__(self, by_service):
        self.by_service = by_service

    def error_rate(self, service, since):
        return self.by_service.get(service)

    def trace(self, trace_id):
        return None


def _cfg():
    cfg = fast_config()
    cfg.metric_services = ["api", "webapp"]
    cfg.metric_error_ratio_threshold = 0.2
    cfg.metric_latency_p95_threshold_ms = 1000.0
    cfg.queue_depth_threshold = 50
    return cfg


# --- error ratio ------------------------------------------------------------------
def test_error_ratio_fires_over_threshold():
    cfg = _cfg()
    src = StubMetrics({'service="api",status=~"5.."': 0.5, 'service="api"': 0.5,
                       'service="webapp"': 0.0})
    anomalies = MetricErrorRatioDetector(cfg, src).check(None, NOW)
    assert [a.service for a in anomalies] == ["api"]
    assert anomalies[0].signal_type == "metric_error_ratio"


def test_error_ratio_silent_when_no_data():
    # source returns None for everything → no anomaly (degrade, not a false positive)
    anomalies = MetricErrorRatioDetector(_cfg(), StubMetrics({})).check(None, NOW)
    assert anomalies == []


# --- latency ----------------------------------------------------------------------
def test_latency_fires_on_high_p95():
    cfg = _cfg()
    src = StubMetrics({'service="api"': 2500.0, 'service="webapp"': 120.0})
    anomalies = MetricLatencyDetector(cfg, src).check(None, NOW)
    assert [a.service for a in anomalies] == ["api"]
    assert "2500ms" in anomalies[0].detail


def test_latency_silent_under_threshold():
    src = StubMetrics({'service="api"': 100.0, 'service="webapp"': 100.0})
    assert MetricLatencyDetector(_cfg(), src).check(None, NOW) == []


# --- saturation -------------------------------------------------------------------
def test_saturation_fires_on_deep_queue():
    anomalies = SaturationDetector(_cfg(), StubMetrics({"queue_depth": 120.0})).check(None, NOW)
    assert len(anomalies) == 1 and anomalies[0].service == "worker"


def test_saturation_silent_on_shallow_queue_or_no_data():
    assert SaturationDetector(_cfg(), StubMetrics({"queue_depth": 3.0})).check(None, NOW) == []
    assert SaturationDetector(_cfg(), StubMetrics({})).check(None, NOW) == []


# --- traces -----------------------------------------------------------------------
def test_trace_error_detector_fires():
    anomalies = TraceErrorDetector(_cfg(), StubTraces({"api": 0.4, "webapp": 0.0})).check(None, NOW)
    assert [a.service for a in anomalies] == ["api"]
    assert anomalies[0].signal_type == "trace_error_rate"


def test_trace_error_detector_silent_when_unavailable():
    assert TraceErrorDetector(_cfg(), StubTraces({})).check(None, NOW) == []
