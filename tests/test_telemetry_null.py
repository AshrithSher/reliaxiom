"""The null test, encoded offline for the telemetry path (invariant #6): an engine carrying
ALL detectors — log, metric, and trace — fed a steadily-healthy system must emit zero
candidates, whether the telemetry backends return healthy values or nothing at all. A false
positive here is the highest-severity bug."""
from datetime import timedelta

from sre_agent.ingest.parser import LineParser
from sre_agent.ingest.window import SlidingWindow
from sre_agent.main import build_engine
from tests.helpers import T0, fast_config, rec


class SilentMetrics:
    """A reachable-but-healthy metrics backend: everything well under threshold."""

    def __init__(self, value=0.0):
        self.value = value

    def instant(self, expr, at):
        return self.value

    def range(self, expr, start, end, step_s):
        return []


class DownMetrics:
    """An unreachable metrics backend: every query degrades to None."""

    def instant(self, expr, at):
        return None

    def range(self, expr, start, end, step_s):
        return []


class SilentTraces:
    def __init__(self, value=0.0):
        self.value = value

    def error_rate(self, service, since):
        return self.value

    def trace(self, trace_id):
        return None


def _run_null(telemetry, ticks=120, step_s=5.0):
    """Feed steady healthy chatter from every service and tick the full engine; return all
    candidates produced (must be empty)."""
    cfg = fast_config()
    engine = build_engine(cfg, LineParser(), telemetry=telemetry)
    window = SlidingWindow(cfg.window_max_age_s)
    services = ["gateway", "webapp", "api", "worker", "loadgen", "auth", "payments"]
    produced = []
    t = 0.0
    for _ in range(ticks):
        for svc in services:
            # healthy lines: a fast request, occasional heartbeat with a shallow queue
            window.append(rec(svc, t, status=200, latency_ms=40))
        window.append(rec("worker", t, event="heartbeat", queue_depth=1))
        now = T0 + timedelta(seconds=t)
        window.prune(now)
        produced.extend(engine.tick(window, now))
        t += step_s
    return produced


def test_null_with_healthy_metrics_and_traces_is_silent():
    telemetry = {"metrics": SilentMetrics(0.0), "traces": SilentTraces(0.0)}
    assert _run_null(telemetry) == []


def test_null_with_metrics_backend_down_is_silent():
    # backend unreachable → adapters return None → metric/trace detectors emit nothing,
    # and the log detectors stay silent on healthy logs
    telemetry = {"metrics": DownMetrics(), "traces": SilentTraces(None)}
    assert _run_null(telemetry) == []


def test_null_log_only_path_still_silent():
    assert _run_null({}) == []
