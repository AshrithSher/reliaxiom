"""Control plane for the demo dashboard — the write side that turns the read-only view into a
single pane of glass: run/stop the agent, inject faults into the lab, and approve/reject Tier-2
actions. The only thing left for a terminal is `docker compose up` for the lab itself.

These actions are orchestration, not detection: fault injection drives the lab's chaos toggles
/ docker, approvals go through the same `IncidentManager.approve/reject` the CLI uses, and the
agent runs as a managed subprocess. None of this changes detection/diagnosis logic.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.action.factory import build_action_backend, build_rate_limiter
from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.changelog import ChangeLog
from sre_agent.config import Config
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology_provider import build_topology_provider
from sre_agent.integrations.notifications import ConsoleNotifier


def _chaos(service: str, scenario: str, action: str) -> list[str]:
    code = ("import urllib.request;"
            f"urllib.request.urlopen(urllib.request.Request("
            f"'http://localhost:5000/chaos/{scenario}/{action}', method='POST'))")
    return ["docker", "exec", service, "python", "-c", code]


# Demo fault catalog. `inject` runs on click; `reset` (below) clears everything.
# auth/payments use chaos toggles (fail fast — stopping an HTTP dependency makes DNS hang);
# worker/postgres/redis use docker stop (no HTTP call in their failure path).
DEMO_FAULTS: dict[str, dict] = {
    "auth-down": {"label": "Auth tier failing", "tier": "auto",
                  "blurb": "auth 5xx → api/webapp/gateway/loadgen cascade → one incident",
                  "inject": [_chaos("auth", "errors", "on")]},
    "payments-down": {"label": "Payments provider down", "tier": "auto",
                      "blurb": "worker can't charge orders → payments:unreachable",
                      "inject": [_chaos("payments", "errors", "on")]},
    "api-errors": {"label": "API 500s", "tier": "auto",
                   "blurb": "api returns ~70% 500s → error_rate cascade",
                   "inject": [_chaos("api", "errors", "on")]},
    "api-latency": {"label": "API latency", "tier": "auto",
                    "blurb": "api adds 2-6s delay → loadgen p95 latency",
                    "inject": [_chaos("api", "latency", "on")]},
    "kill-worker": {"label": "Worker stopped", "tier": "auto",
                    "blurb": "the silent fault: worker stops logging → auto-restart",
                    "inject": [["docker", "stop", "worker"]]},
    "db-down": {"label": "Postgres down", "tier": "approval",
                "blurb": "stateful → restart needs human approval (Tier-2)",
                "inject": [["docker", "stop", "postgres"]]},
    "redis-down": {"label": "Redis down", "tier": "approval",
                   "blurb": "stateful → restart needs human approval (Tier-2)",
                   "inject": [["docker", "stop", "redis"]]},
}

# Reset: clear every chaos toggle and restart any stopped container.
_RESET_CMDS: list[list[str]] = [
    _chaos("auth", "errors", "off"), _chaos("payments", "errors", "off"),
    _chaos("api", "errors", "off"), _chaos("api", "latency", "off"),
    ["docker", "start", "worker"], ["docker", "start", "postgres"], ["docker", "start", "redis"],
]


def run_fault(name: str, compose_dir: str) -> dict:
    """Inject a named demo fault. Returns {ok, name, errors}."""
    fault = DEMO_FAULTS.get(name)
    if fault is None:
        return {"ok": False, "name": name, "error": "unknown fault"}
    errors = _run_all(fault["inject"], compose_dir)
    return {"ok": not errors, "name": name, "errors": errors}


def reset_lab(compose_dir: str) -> dict:
    """Clear all chaos and restart any stopped containers (best-effort)."""
    errors = _run_all(_RESET_CMDS, compose_dir, ignore_errors=True)
    return {"ok": True, "errors": errors}


def _run_all(cmds: list[list[str]], cwd: str, ignore_errors: bool = False) -> list[str]:
    errors: list[str] = []
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0 and not ignore_errors:
                errors.append(f"{' '.join(cmd[:3])}…: {r.stderr.strip()[:160]}")
        except Exception as exc:  # noqa: BLE001
            if not ignore_errors:
                errors.append(f"{' '.join(cmd[:3])}…: {exc}")
    return errors


class AgentRunner:
    """Run the SRE agent as a managed subprocess so the dashboard can start/stop it and stream
    its log. Launched with --execute --jira --confluence so the demo acts for real and writes
    to Atlassian. Single instance."""

    def __init__(self, repo_root: Path, config_path: str, python_exe: str | None = None) -> None:
        self._repo = repo_root
        self._config = config_path
        self._python = python_exe or sys.executable
        self._proc: subprocess.Popen[str] | None = None
        self._log: deque[str] = deque(maxlen=600)
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> dict:
        if self.running:
            return {"running": True, "pid": self._proc.pid, "already": True}
        cmd = [self._python, "-m", "sre_agent.main", "--config", self._config,
               "--execute", "--jira", "--confluence"]
        self._proc = subprocess.Popen(
            cmd, cwd=str(self._repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        with self._lock:
            self._log.clear()
            self._log.append(f"$ {' '.join(cmd)}")
        threading.Thread(target=self._pump, args=(self._proc.stdout,), daemon=True).start()
        return {"running": True, "pid": self._proc.pid}

    def stop(self) -> dict:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        return {"running": False}

    def status(self) -> dict:
        return {"running": self.running,
                "pid": self._proc.pid if self.running else None}

    def logs(self) -> list[str]:
        with self._lock:
            return list(self._log)

    def _pump(self, stream) -> None:
        for line in stream:
            with self._lock:
                self._log.append(line.rstrip())


def _approval_manager(cfg: Config, ticket_store, postmortems) -> IncidentManager:
    """A manager wired to execute an approved/rejected action against persisted state — the
    same shape the approve CLI uses, but sharing the dashboard's Jira-backed stores."""
    data_dir = Path(cfg.data_dir)
    changelog = ChangeLog(data_dir / "changes.db")
    return IncidentManager(
        IncidentStore(data_dir / "incidents.db"), ticket_store, ConsoleNotifier(),
        build_topology_provider(cfg).topology(), cfg,
        executor=ActionExecutor(backend=build_action_backend(cfg), dry_run=False),
        guardrails=Guardrails(),
        recovery=RecoveryEvaluator(cfg), changelog=changelog, postmortems=postmortems,
        ratelimiter=build_rate_limiter(cfg, changelog))


def approve_incident(cfg: Config, ticket_store, postmortems, incident_id: str,
                     approver: str) -> dict:
    mgr = _approval_manager(cfg, ticket_store, postmortems)
    now = datetime.now(timezone.utc)
    result = mgr.approve(incident_id, approver=approver, now=now)
    if result is None:
        return {"ok": False, "error": f"{incident_id} is not awaiting approval"}
    return {"ok": bool(result.success), "detail": result.detail}


def reject_incident(cfg: Config, ticket_store, postmortems, incident_id: str,
                    approver: str) -> dict:
    mgr = _approval_manager(cfg, ticket_store, postmortems)
    now = datetime.now(timezone.utc)
    mgr.reject(incident_id, approver=approver, now=now)
    return {"ok": True, "detail": f"{incident_id} rejected → escalated"}


def resolve_incident(cfg: Config, ticket_store, postmortems, incident_id: str,
                     resolver: str) -> dict:
    """Human closure for an escalated/flapping incident the operator fixed out-of-band — the
    'I fixed it myself, close the ticket' action the UI was missing. The agent never auto-closes
    a handed-off incident, so this is the only way one leaves the board once escalated."""
    mgr = _approval_manager(cfg, ticket_store, postmortems)
    now = datetime.now(timezone.utc)
    inc = mgr.manual_resolve(incident_id, resolver=resolver, now=now)
    if inc is None:
        return {"ok": False, "error": f"{incident_id} is not escalated/flapping — nothing to close"}
    return {"ok": True, "detail": f"{incident_id} resolved by {resolver}"}
