"""Regression: the approve/reject CLI must not crash on a narrow (cp1252) console.

Found in live end-to-end testing — `reject` drove `_escalate`, whose ConsoleNotifier prints a
line containing '→'; on a Windows cp1252 console that raised UnicodeEncodeError *before* the
incident state was persisted, so the escalation was silently lost. No in-process unit test caught
it because pytest's stdout isn't a cp1252 console. This reproduces the exact condition in a
subprocess with PYTHONIOENCODING=cp1252 and asserts the CLI completes AND persists the transition.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore
from sre_agent.integrations.ticketing import SqliteTicketStore, TicketStatus

T0 = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _seed_awaiting_incident(data_dir) -> None:
    tickets = SqliteTicketStore(data_dir / "tickets.db")
    t = tickets.create(fingerprint="postgres:unreachable", title="postgres unreachable",
                       services=["postgres", "api"], fault_type="unreachable", severity="Low",
                       evidence="e", now=T0)
    tickets.set_status(t.id, TicketStatus.AWAITING_APPROVAL, now=T0)
    store = IncidentStore(data_dir / "incidents.db")
    store.save(Incident(
        id="INC-1", fingerprint="postgres:unreachable", ticket_id=t.id,
        state=IncidentState.AWAITING_APPROVAL, root_service="postgres",
        services=["postgres", "api"], fault_type="unreachable", severity="Low",
        first_seen=T0, updated_at=T0, pending_action="restart_container",
        pending_params={"service": "postgres"}, approval_deadline=T0))


def test_reject_cli_survives_cp1252_console_and_persists_escalation(tmp_path):
    _seed_awaiting_incident(tmp_path)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"data_dir": str(tmp_path), "ticketing_backend": "sqlite",
                               "postmortems_backend": "local"}), encoding="utf-8")

    env = {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0",
           "PATH": __import__("os").environ.get("PATH", "")}
    proc = subprocess.run(
        [sys.executable, "-m", "sre_agent.approve", "reject", "INC-1", "--by", "t",
         "--config", str(cfg)],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace")

    assert proc.returncode == 0, f"CLI crashed: {proc.stderr}"
    # the escalation must have been persisted (the bug lost it when the notifier print crashed)
    assert IncidentStore(tmp_path / "incidents.db").get("INC-1").state is IncidentState.ESCALATED
