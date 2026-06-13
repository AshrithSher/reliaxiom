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
}

# Deferred to M2 — multi-signal faults the current harness cannot fairly score.
# `latency` (2-6s api delay) trips latency_p95 on loadgen AND error_rate on webapp/gateway
# (504 timeouts); `memleak` trips crash_loop on api AND error_rate on its consumers during
# the OOM gap. The harness today flags every non-primary candidate as a failure, so these
# only score correctly once M2's topology correlation collapses them into one incident with
# a single primary signal. Until then these detectors are covered by unit tests
# (test_latency.py, test_crashloop.py, test_queue_depth.py). See DECISIONS.md D-014.
#
# queue_growth has no direct chaos scenario: kill-worker kills the heartbeats too (silence
# catches it), and there is no "slow worker" toggle. Unit-tested only, by design.
DEFERRED_TO_M2 = ["latency", "memleak"]
