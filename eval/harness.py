"""Evaluation harness (M0 skeleton).

Runs the agent in-process against the live lab, injects a labeled fault, and scores:
  - detected?  time-to-detect?
  - correct signal_type and service vs. ground truth?
  - false positives before injection / from other signals?

Also provides the null test: a healthy system for N seconds must produce zero candidates.

Usage:
    python -m eval.harness --scenario errors
    python -m eval.harness --scenario kill-worker
    python -m eval.harness --null --duration 3600
    python -m eval.harness --all
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from eval.scenarios import SCENARIOS, Scenario
from sre_agent.config import Config
from sre_agent.detect.engine import DetectionEngine
from sre_agent.ingest.parser import LineParser
from sre_agent.ingest.tailer import Tailer
from sre_agent.ingest.window import SlidingWindow
from sre_agent.main import build_engine
from sre_agent.models import IncidentCandidate


@dataclass
class RunResult:
    scenario: str
    passed: bool
    detected: bool = False
    time_to_detect_s: float | None = None
    candidates: list[IncidentCandidate] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def report(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"[{status}] {self.scenario}"]
        if self.detected:
            lines.append(f"  detected in {self.time_to_detect_s:.0f}s")
        for c in self.candidates:
            lines.append(f"  candidate: {c.fingerprint()} — {c.detail}")
        for f in self.failures:
            lines.append(f"  failure: {f}")
        return "\n".join(lines)


class _LiveAgent:
    """The M0 agent wired up in-process so the harness can observe candidates directly."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.window = SlidingWindow(cfg.window_max_age_s)
        self.parser = LineParser()
        self.tailer = Tailer(self.window, self.parser)
        self.engine: DetectionEngine = build_engine(cfg, self.parser)

    def start(self) -> None:
        self.tailer.start_docker(self.cfg.compose_dir)

    def stop(self) -> None:
        self.tailer.stop()

    def tick(self) -> list[IncidentCandidate]:
        now = datetime.now(timezone.utc)
        self.window.prune(now)
        return self.engine.tick(self.window, now)


def _run(cmds: list[list[str]], cwd: str) -> None:
    for cmd in cmds:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"command failed: {' '.join(cmd[:3])}…\n{result.stderr}")


def run_scenario(scenario: Scenario, cfg: Config, settle_s: float = 30.0) -> RunResult:
    result = RunResult(scenario=scenario.name, passed=False)
    agent = _LiveAgent(cfg)
    agent.start()
    print(f"--- scenario {scenario.name}: settling {settle_s:.0f}s on healthy traffic", flush=True)
    try:
        # pre-injection quiet period: any candidate here is a false positive
        deadline = time.time() + settle_s
        while time.time() < deadline:
            time.sleep(cfg.tick_interval_s)
            for c in agent.tick():
                result.candidates.append(c)
                result.failures.append(f"false positive before injection: {c.fingerprint()}")
        if result.failures:
            return result

        print(f"--- injecting fault: {scenario.name}", flush=True)
        _run(scenario.inject, cfg.compose_dir)
        t0 = time.time()

        deadline = t0 + scenario.detect_timeout_s
        while time.time() < deadline and not result.detected:
            time.sleep(cfg.tick_interval_s)
            for c in agent.tick():
                result.candidates.append(c)
                service_match = any(s in scenario.expected_services for s in c.services)
                if c.signal_type == scenario.expected_signal and service_match:
                    result.detected = True
                    result.time_to_detect_s = time.time() - t0
                else:
                    result.failures.append(f"unexpected candidate: {c.fingerprint()}")

        if not result.detected:
            result.failures.append(
                f"not detected within {scenario.detect_timeout_s:.0f}s "
                f"(expected {scenario.expected_signal} on {scenario.expected_services})")
        result.passed = result.detected and not result.failures
        return result
    finally:
        print(f"--- restoring: {scenario.name}", flush=True)
        try:
            _run(scenario.restore, cfg.compose_dir)
        except RuntimeError as exc:
            print(f"!!! restore failed, heal manually: {exc}", file=sys.stderr)
        agent.stop()


def run_null_test(cfg: Config, duration_s: float) -> RunResult:
    result = RunResult(scenario=f"null-test-{duration_s:.0f}s", passed=False)
    agent = _LiveAgent(cfg)
    agent.start()
    print(f"--- null test: watching healthy system for {duration_s:.0f}s; any output is a failure",
          flush=True)
    try:
        deadline = time.time() + duration_s
        while time.time() < deadline:
            time.sleep(cfg.tick_interval_s)
            for c in agent.tick():
                result.candidates.append(c)
                result.failures.append(f"false positive: {c.fingerprint()} — {c.detail}")
        result.passed = not result.failures
        result.detected = False
        return result
    finally:
        agent.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SRE agent eval harness")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), help="run one labeled fault")
    ap.add_argument("--all", action="store_true", help="run every scenario")
    ap.add_argument("--null", action="store_true", help="run the null (silence) test")
    ap.add_argument("--duration", type=float, default=3600, help="null test duration seconds")
    ap.add_argument("--config", help="JSON file with Config overrides")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    results: list[RunResult] = []

    if args.null:
        results.append(run_null_test(cfg, args.duration))
    if args.scenario:
        results.append(run_scenario(SCENARIOS[args.scenario], cfg))
    if args.all:
        for name in sorted(SCENARIOS):
            results.append(run_scenario(SCENARIOS[name], cfg))
            time.sleep(30)  # let the system settle between faults

    if not results:
        ap.error("nothing to run: pass --scenario, --all, or --null")

    print("\n=== results ===")
    for r in results:
        print(r.report())
    summary = {"passed": sum(r.passed for r in results), "total": len(results)}
    print(json.dumps(summary))
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
