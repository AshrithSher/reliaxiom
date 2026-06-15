"""Labeled fault scenarios with ground truth. Each maps to one chaos action in the lab
(chaos/chaos.sh equivalents, run as direct docker commands so they work on Windows)."""
from __future__ import annotations

from dataclasses import dataclass, field


def _chaos_toggle(scenario: str, action: str, service: str = "api") -> list[str]:
    code = (
        "import urllib.request\n"
        f"req = urllib.request.Request('http://localhost:5000/chaos/{scenario}/{action}', method='POST')\n"
        "print(urllib.request.urlopen(req).read().decode())"
    )
    return ["docker", "exec", service, "python", "-c", code]


@dataclass
class Scenario:
    name: str
    inject: list[list[str]]          # commands to run at t0
    restore: list[list[str]]         # commands to run at teardown (always)
    expected_signal: str             # signal_type the candidate must carry
    expected_services: list[str]     # at least one must appear in candidate.services
    detect_timeout_s: float = 240.0  # exit criterion: detected within this budget
    notes: str = ""
    extra_config: dict = field(default_factory=dict)
    # signal_types that may legitimately co-fire and must NOT be scored as a false positive.
    # A multi-signal fault (e.g. `errors` trips both the log error_rate and the metric 5xx
    # ratio) collapses to one incident in the manager, but at the detector level both fire;
    # this lets a metric-path scenario accept its log twin (and vice versa).
    allow_signals: list[str] = field(default_factory=list)


SCENARIOS: dict[str, Scenario] = {
    "errors": Scenario(
        name="errors",
        inject=[_chaos_toggle("errors", "on")],
        restore=[_chaos_toggle("errors", "off")],
        expected_signal="error_rate",
        # the fault cascades to every consumer, including loadgen — the synthetic
        # user seeing 500s is the user-facing signal, not a false positive
        expected_services=["api", "webapp", "gateway", "loadgen"],
        notes="api 500-rate jumps to ~70%; error_rate detector must fire",
    ),
    "kill-worker": Scenario(
        name="kill-worker",
        inject=[["docker", "stop", "worker"]],
        restore=[["docker", "start", "worker"]],
        expected_signal="silence",
        expected_services=["worker"],
        notes="the silent fault: worker stops logging, no errors anywhere",
    ),
    "db-down": Scenario(
        name="db-down",
        # stop the postgres container outright. The container-down detector fires from the
        # polled signal regardless of how much the consumers log — this is the outage that the
        # log-only path misses when a broken auth tier short-circuits db access, or when the
        # only consumer (a slow-retry worker) never trips the error-rate threshold.
        inject=[["docker", "stop", "postgres"]],
        restore=[["docker", "start", "postgres"]],
        expected_signal="container_down",
        expected_services=["postgres"],
        # downstream consumers may flood db_unreachable and co-fire error_rate; the manager
        # collapses every one onto the same postgres:unreachable incident (invariant #3).
        allow_signals=["error_rate", "metric_error_ratio", "trace_error_rate",
                       "silence", "queue_growth", "metric_saturation"],
        notes="postgres stopped: container-down detector tickets it even with no log flood; "
              "consumers' error_rate collapses into one postgres:unreachable incident",
    ),
    "auth-down": Scenario(
        name="auth-down",
        # the auth tier returns errors (stays up, so the api fails fast with auth_unreachable
        # rather than hanging on DNS for a stopped container). api + every consumer 503s;
        # correlation collapses the fan-out to one auth:unreachable incident rooted at auth.
        inject=[_chaos_toggle("errors", "on", "auth")],
        restore=[_chaos_toggle("errors", "off", "auth")],
        expected_signal="error_rate",
        # auth itself logs the failures (it is the root), plus the whole fan-out errors
        expected_services=["auth", "api", "webapp", "gateway", "loadgen"],
        notes="auth tier failing: api emits auth_unreachable, cascade rooted at auth; "
              "remediation restart_container auth resets the (in-memory) fault",
    ),
    "payments-down": Scenario(
        name="payments-down",
        inject=[_chaos_toggle("errors", "on", "payments")],
        restore=[_chaos_toggle("errors", "off", "payments")],
        expected_signal="error_rate",
        # payments itself logs the declines (root), and the worker errors trying to charge
        expected_services=["payments", "worker"],
        notes="payments provider failing: worker can't charge orders, emits payments_unreachable",
    ),
    # --- metric/trace-path scenarios (require the LGTM stack up: telemetry_metrics/traces) ---
    "errors-red": Scenario(
        name="errors-red",
        inject=[_chaos_toggle("errors", "on")],
        restore=[_chaos_toggle("errors", "off")],
        expected_signal="metric_error_ratio",   # the RED-errors metric, from Prometheus
        expected_services=["api", "webapp"],
        extra_config={"telemetry_metrics": "prometheus", "telemetry_traces": "tempo"},
        allow_signals=["error_rate", "trace_error_rate", "latency_p95", "metric_latency_p95"],
        notes="same fault as `errors`, scored on the Prometheus 5xx ratio rather than log counts",
    ),
    "latency-red": Scenario(
        name="latency-red",
        inject=[_chaos_toggle("latency", "on")],
        restore=[_chaos_toggle("latency", "off")],
        # the histogram p95 gives a clean single metric signal where the log-derived path was
        # too multi-signal to score (D-014) — this is that deferral's metric-path resolution
        expected_signal="metric_latency_p95",
        # the api delay legitimately cascades to its caller webapp (webapp→api), so webapp's
        # histogram p95 breaches too — both are the SAME fault, which correlation (D-040)
        # collapses into one metric_latency_p95 incident. List both like `errors` lists its
        # cascade, so the upstream twin isn't scored as a false positive.
        expected_services=["api", "webapp"],
        extra_config={"telemetry_metrics": "prometheus"},
        allow_signals=["latency_p95", "error_rate", "metric_error_ratio"],
        notes="2-6s api delay → real histogram p95 breaches threshold from Prometheus; "
              "cascades to webapp's p95 too (one fault, collapsed by correlation)",
    ),
}

# Deferred multi-signal faults: latency / memleak. `latency` (2-6s api delay) trips latency_p95
# on loadgen AND error_rate on webapp/gateway (504 timeouts); `memleak` trips crash_loop on api
# AND error_rate on its consumers during the OOM gap. As of D-040 the multi-signal correlation
# engine DOES collapse these into one incident (same-service + topology + shared-trace fusion),
# proven offline (test_correlation.py, test_incident_manager.py). The remaining work is harness
# wiring: a live scenario scored by PRIMARY signal (allow_signals carries the twins), validated
# with the lab up. Until that live wiring lands these stay unit-tested (test_latency.py,
# test_crashloop.py, test_queue_depth.py). See DECISIONS.md D-014 (gap) and D-040 (engine).
#
# queue_growth has no direct chaos scenario: kill-worker kills the heartbeats too (silence
# catches it), and there is no "slow worker" toggle. Unit-tested only, by design.
DEFERRED_TO_M2 = ["latency", "memleak"]
