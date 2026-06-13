"""Diagnosis-accuracy evaluation (M3 exit criterion: root cause correct on >= 4/5 scenarios
vs ground truth; unknown/ungroundable fault -> clean Tier-3 escalation).

Decoupled from detection timing: inject a labeled fault, capture the lab's real recent logs
into a window, construct the incident from ground truth, run the real diagnosis, and score
the root cause against expected keywords. The scoring function is unit-testable offline; the
live runner exercises the real LLM end to end.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sre_agent.config import Config
from sre_agent.diagnosis.context import ContextAssembler
from sre_agent.diagnosis.diagnoser import Diagnoser
from sre_agent.diagnosis.llm import LLMProvider
from sre_agent.diagnosis.schema import Diagnosis
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.ingest.parser import LineParser
from sre_agent.ingest.window import SlidingWindow


def _chaos(scenario: str, action: str) -> list[str]:
    code = (f"import urllib.request;urllib.request.urlopen(urllib.request.Request("
            f"'http://localhost:5000/chaos/{scenario}/{action}', method='POST'))")
    return ["docker", "exec", "api", "python", "-c", code]


@dataclass
class DiagnosisCase:
    name: str
    fingerprint: str
    root_service: str
    services: list[str]
    fault_type: str
    expected_keywords: list[str]        # any-of, matched in root_cause/action (lowercased)
    inject: list[list[str]] = field(default_factory=list)
    restore: list[list[str]] = field(default_factory=list)
    expect_escalate: bool = False
    settle_s: float = 40.0


CASES: list[DiagnosisCase] = [
    DiagnosisCase("kill-redis", "redis:unreachable", "redis", ["worker"], "unreachable",
                  expected_keywords=["redis", "cache", "queue"],
                  inject=[["docker", "stop", "redis"]], restore=[["docker", "start", "redis"]]),
    DiagnosisCase("kill-db", "postgres:unreachable", "postgres", ["api", "worker"], "unreachable",
                  expected_keywords=["postgres", "database", "db", "connection"],
                  inject=[["docker", "stop", "postgres"]], restore=[["docker", "start", "postgres"]]),
    DiagnosisCase("errors", "api:internal_error", "api", ["api"], "internal_error",
                  expected_keywords=["api", "error", "500", "exception", "internal"],
                  inject=[_chaos("errors", "on")], restore=[_chaos("errors", "off")]),
    DiagnosisCase("kill-worker", "worker:silence", "worker", ["worker"], "silence",
                  expected_keywords=["worker", "stopped", "hung", "unresponsive", "down", "crash"],
                  inject=[["docker", "stop", "worker"]], restore=[["docker", "start", "worker"]]),
    # ungroundable: no fault injected, so any cited error evidence is fabricated -> the guards
    # must force a clean Tier-3 escalation rather than a confident wrong answer.
    DiagnosisCase("ungroundable", "api:phantom", "api", ["api"], "phantom",
                  expected_keywords=[], expect_escalate=True, settle_s=5.0),
]


def score(diagnosis: Diagnosis, case: DiagnosisCase) -> bool:
    if case.expect_escalate:
        return diagnosis.escalate
    if diagnosis.escalate:
        return False
    text = (diagnosis.root_cause + " " + diagnosis.selected_action + " "
            + json.dumps(diagnosis.action_params)).lower()
    return any(kw in text for kw in case.expected_keywords)


def _capture_window(cfg: Config, lookback_s: float, now: datetime) -> SlidingWindow:
    out = subprocess.run(
        ["docker", "compose", "logs", "--no-color", "--no-log-prefix", "--since",
         f"{int(lookback_s)}s"],
        cwd=cfg.compose_dir, capture_output=True, text=True, encoding="utf-8", errors="replace")
    parser = LineParser()
    window = SlidingWindow(cfg.window_max_age_s)
    for line in out.stdout.splitlines():
        rec = parser.parse(line, now=now)
        if rec is not None:
            window.append(rec)
    return window


def _incident(case: DiagnosisCase, now: datetime) -> Incident:
    return Incident(id=f"INC-{case.name}", fingerprint=case.fingerprint, ticket_id=None,
                    state=IncidentState.DETECTED, root_service=case.root_service,
                    services=case.services, fault_type=case.fault_type, severity="High",
                    first_seen=now, updated_at=now)


def _ensure_healthy(cfg: Config, sleep, timeout_s: float = 90.0) -> bool:
    """Block until the lab is fully recovered, so the previous scenario's logs can't
    contaminate the next one's window. Polls the gateway and container states."""
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        ps = subprocess.run(["docker", "compose", "ps", "--format", "{{.Name}} {{.Status}}"],
                            cwd=cfg.compose_dir, capture_output=True, text=True)
        all_up = ps.stdout.count("Up") >= 7
        curl = subprocess.run(["docker", "exec", "api", "python", "-c",
                               "import urllib.request;print(urllib.request.urlopen("
                               "'http://localhost:5000/products').status)"],
                              cwd=cfg.compose_dir, capture_output=True, text=True)
        if all_up and "200" in curl.stdout:
            sleep(8)   # a little extra so the recovery tail ages out of the capture window
            return True
        sleep(5)
    return False


def run_live(cases: list[DiagnosisCase], cfg: Config, provider: LLMProvider, sleep=None) -> dict:
    import time
    sleep = sleep or time.sleep
    diagnoser = Diagnoser(provider, runs=cfg.diagnosis_runs, temperature=cfg.llm_temperature)
    assembler = ContextAssembler(LAB_TOPOLOGY, cfg)
    results = {}
    for case in cases:
        _ensure_healthy(cfg, sleep)               # clean slate before injecting
        for cmd in case.inject:
            subprocess.run(cmd, cwd=cfg.compose_dir, capture_output=True, text=True)
        try:
            sleep(case.settle_s)
            now = datetime.now(timezone.utc)
            # capture ONLY this scenario's window (since injection), no prior-scenario bleed
            window = _capture_window(cfg, case.settle_s + 5, now)
            inc = _incident(case, now)
            system, user = assembler.build(inc, window, now)
            verifier = assembler.make_verifier(system + "\n" + user)
            dx = diagnoser.diagnose(system=system, user=user, verify_evidence=verifier,
                                    restart_target=inc.root_service)
            results[case.name] = {"pass": score(dx, case), "escalate": dx.escalate,
                                  "root_cause": dx.root_cause[:160], "action": dx.selected_action,
                                  "reasons": dx.escalation_reasons}
        finally:
            for cmd in case.restore:
                subprocess.run(cmd, cwd=cfg.compose_dir, capture_output=True, text=True)
            sleep(5)
    passed = sum(1 for r in results.values() if r["pass"])
    results["_summary"] = {"passed": passed, "total": len(cases)}
    return results


if __name__ == "__main__":
    from sre_agent.main import build_provider, load_secrets

    cfg = Config()
    load_secrets()
    provider = build_provider(cfg)
    if provider is None:
        raise RuntimeError(
            f"no LLM provider available for llm_provider={cfg.llm_provider!r} "
            f"(check the API key in .secrets/llm.env)")
    results = run_live(CASES, cfg, provider)
    print(json.dumps(results, indent=2))
    raise SystemExit(0 if results["_summary"]["passed"] >= 4 else 1)
