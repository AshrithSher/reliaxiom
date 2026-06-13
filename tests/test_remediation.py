"""TDD: the act → verify loop (M4). The manager executes a Tier-1 AUTO action under
guardrails, tags it in the change log, then verifies recovery: recovered → RESOLVED;
no recovery in the window → re-diagnose (bounded) → ESCALATE. The agent never acts on a
non-AUTO tier here, and a blocked guardrail escalates rather than forcing the action."""
from datetime import timedelta

import pytest

from sre_agent.action.catalog import Tier
from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.changelog import ChangeLog
from sre_agent.config import Config
from sre_agent.diagnosis.schema import Diagnosis
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.notifications import InMemoryNotifier
from sre_agent.integrations.ticketing import SqliteTicketStore, TicketStatus
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, rec


class FakeRunner:
    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return self.rc, "ok", ""


def cfg():
    c = Config()
    c.recovery_window_s = 60.0
    c.max_remediation_loops = 2
    c.silence_threshold_s = 30.0
    return c


def build(tmp_path, runner=None):
    c = cfg()
    tickets = SqliteTicketStore(tmp_path / "t.db")
    incidents = IncidentStore(tmp_path / "i.db")
    notifier = InMemoryNotifier()
    changelog = ChangeLog(tmp_path / "c.db")
    mgr = IncidentManager(incidents, tickets, notifier, LAB_TOPOLOGY, c,
                          executor=ActionExecutor(runner=runner or FakeRunner(), dry_run=False),
                          guardrails=Guardrails(c.max_restarts_per_hour, changelog),
                          recovery=RecoveryEvaluator(c), changelog=changelog)
    return mgr, tickets, incidents, notifier, changelog


def diagnosed_incident(tickets, incidents, fault="silence", service="worker"):
    ticket = tickets.create(fingerprint=f"{service}:{fault}", title="x", services=[service],
                            fault_type=fault, severity="Medium", evidence="x", now=T0)
    inc = Incident(id="INC-1", fingerprint=f"{service}:{fault}", ticket_id=ticket.id,
                   state=IncidentState.DIAGNOSING, root_service=service, services=[service],
                   fault_type=fault, severity="Medium", first_seen=T0, updated_at=T0)
    incidents.save(inc)
    return inc


def auto_dx(action="restart_container", params=None):
    return Diagnosis(root_cause="worker hung", evidence=["e"], selected_action=action,
                     action_params=params or {"service": "worker"}, alternatives=[],
                     tier=Tier.AUTO, escalate=False, escalation_reasons=[])


def at(s):
    return T0 + timedelta(seconds=s)


# --- act -------------------------------------------------------------------------
def test_act_executes_and_enters_verifying(tmp_path):
    runner = FakeRunner()
    mgr, tickets, incidents, _, changelog = build(tmp_path, runner)
    inc = diagnosed_incident(tickets, incidents)
    result = mgr.act(inc, auto_dx(), now=at(10))
    assert result.success and runner.calls == [["docker", "restart", "worker"]]
    assert incidents.get("INC-1").state is IncidentState.VERIFYING
    # action tagged in the change log BEFORE detection could react (invariant #5)
    tagged = changelog.recent(at(0), at(20))
    assert any(e.actor == "sre-agent" and e.service == "worker" for e in tagged)


def test_act_blocked_by_guardrail_escalates(tmp_path):
    mgr, tickets, incidents, notifier, changelog = build(tmp_path)
    # pre-load the restart cap for worker
    from sre_agent.changelog import ChangeLogEntry
    for i in range(3):
        changelog.record(ChangeLogEntry(ts=at(1 + i), actor="sre-agent", service="worker",
                                        change_type="restart_container", detail="x"))
    inc = diagnosed_incident(tickets, incidents)
    result = mgr.act(inc, auto_dx(), now=at(10))
    assert result is None
    assert incidents.get("INC-1").state is IncidentState.ESCALATED
    assert tickets.get(inc.ticket_id).status is TicketStatus.ESCALATED


# --- verify ----------------------------------------------------------------------
def verifying_incident(tickets, incidents, started_at):
    inc = diagnosed_incident(tickets, incidents)
    inc.state = IncidentState.VERIFYING
    inc.verify_started_at = started_at
    incidents.save(inc)
    return inc


def test_verify_resolves_on_recovery(tmp_path):
    mgr, tickets, incidents, notifier, _ = build(tmp_path)
    inc = verifying_incident(tickets, incidents, started_at=at(10))
    window = SlidingWindow()
    window.append(rec("worker", 95))   # worker logging again
    outcome = mgr.verify_incident(inc, window, None, now=at(100))
    assert outcome == "resolved"
    assert incidents.get("INC-1").state is IncidentState.RESOLVED
    assert tickets.get(inc.ticket_id).status is TicketStatus.RESOLVED


def test_verify_waits_within_window(tmp_path):
    mgr, tickets, incidents, _, _ = build(tmp_path)
    inc = verifying_incident(tickets, incidents, started_at=at(80))
    window = SlidingWindow()
    window.append(rec("worker", 10))   # still silent
    assert mgr.verify_incident(inc, window, None, now=at(100)) == "waiting"
    assert incidents.get("INC-1").state is IncidentState.VERIFYING


def test_verify_retries_then_escalates(tmp_path):
    mgr, tickets, incidents, _, _ = build(tmp_path)
    inc = verifying_incident(tickets, incidents, started_at=at(0))
    silent = SlidingWindow()
    silent.append(rec("worker", 0))    # never recovers
    # first timeout → retry (back to diagnosing)
    assert mgr.verify_incident(inc, silent, None, now=at(70)) == "retry"
    assert incidents.get("INC-1").state is IncidentState.DIAGNOSING
    # simulate a second act → verify that also times out → escalate
    inc2 = incidents.get("INC-1")
    inc2.state = IncidentState.VERIFYING
    inc2.verify_started_at = at(80)
    incidents.save(inc2)
    assert mgr.verify_incident(inc2, silent, None, now=at(150)) == "escalated"
    assert incidents.get("INC-1").state is IncidentState.ESCALATED


def test_does_not_act_on_non_auto_tier(tmp_path):
    runner = FakeRunner()
    mgr, tickets, incidents, _, _ = build(tmp_path, runner)
    inc = diagnosed_incident(tickets, incidents)
    approval_dx = auto_dx(params={"service": "redis"})  # restart redis = APPROVAL tier
    approval_dx.tier = Tier.APPROVAL
    result = mgr.act(inc, approval_dx, now=at(10))
    assert result is None and runner.calls == []        # not executed
    assert incidents.get("INC-1").state is IncidentState.ESCALATED
