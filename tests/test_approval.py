"""TDD: HITL approval (M5, Tier 2). A diagnosis that selects an APPROVAL-tier action is NOT
auto-executed — the agent posts a proposal (with blast radius) and waits. A human approve
runs the action and records who approved; reject escalates; a 15-min timeout escalates with
NO action taken."""
from datetime import timedelta

import pytest

from sre_agent.action.catalog import Tier
from sre_agent.action.backend import DockerActionBackend
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
from tests.helpers import T0


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return 0, "ok", ""


def cfg():
    c = Config()
    c.approval_timeout_s = 900.0
    return c


def build(tmp_path):
    c = cfg()
    tickets = SqliteTicketStore(tmp_path / "t.db")
    incidents = IncidentStore(tmp_path / "i.db")
    notifier = InMemoryNotifier()
    changelog = ChangeLog(tmp_path / "c.db")
    runner = FakeRunner()
    mgr = IncidentManager(incidents, tickets, notifier, LAB_TOPOLOGY, c,
                          executor=ActionExecutor(backend=DockerActionBackend(runner=runner), dry_run=False),
                          guardrails=Guardrails(),
                          recovery=RecoveryEvaluator(c), changelog=changelog)
    return mgr, tickets, incidents, notifier, runner


def diagnosing_incident(tickets, incidents, root="redis", fault="unreachable"):
    ticket = tickets.create(fingerprint=f"{root}:{fault}", title="x", services=["api", "worker"],
                            fault_type=fault, severity="High", evidence="x", now=T0)
    inc = Incident(id="INC-1", fingerprint=f"{root}:{fault}", ticket_id=ticket.id,
                   state=IncidentState.DIAGNOSING, root_service=root, services=["api", "worker"],
                   fault_type=fault, severity="High", first_seen=T0, updated_at=T0)
    incidents.save(inc)
    return inc


def approval_dx():
    # restart redis is Tier-2 APPROVAL
    return Diagnosis(root_cause="redis stuck", evidence=["e"], selected_action="restart_container",
                     action_params={"service": "redis"}, alternatives=[], tier=Tier.APPROVAL,
                     escalate=False, escalation_reasons=[])


def at(s):
    return T0 + timedelta(seconds=s)


def test_approval_action_requests_not_executes(tmp_path):
    mgr, tickets, incidents, notifier, runner = build(tmp_path)
    inc = diagnosing_incident(tickets, incidents)
    mgr.request_approval(inc, approval_dx(), now=at(10))
    stored = incidents.get("INC-1")
    assert stored.state is IncidentState.AWAITING_APPROVAL
    assert stored.pending_action == "restart_container" and stored.pending_params == {"service": "redis"}
    assert runner.calls == []                               # nothing executed yet
    assert notifier.sent[-1].kind == "approval"
    assert "redis" in notifier.sent[-1].body                # blast radius mentions target
    assert tickets.get(inc.ticket_id).status is TicketStatus.AWAITING_APPROVAL


def test_approve_executes_and_records_approver(tmp_path):
    mgr, tickets, incidents, _, runner = build(tmp_path)
    inc = diagnosing_incident(tickets, incidents)
    mgr.request_approval(inc, approval_dx(), now=at(10))
    result = mgr.approve("INC-1", approver="alice@example.com", now=at(60))
    assert result.success and runner.calls == [["docker", "restart", "redis"]]
    assert incidents.get("INC-1").state is IncidentState.VERIFYING
    comments = " ".join(c.body for c in tickets.get(inc.ticket_id).comments)
    assert "alice@example.com" in comments


def test_reject_escalates_without_acting(tmp_path):
    mgr, tickets, incidents, _, runner = build(tmp_path)
    inc = diagnosing_incident(tickets, incidents)
    mgr.request_approval(inc, approval_dx(), now=at(10))
    mgr.reject("INC-1", approver="bob@example.com", now=at(60))
    assert incidents.get("INC-1").state is IncidentState.ESCALATED
    assert runner.calls == []
    assert tickets.get(inc.ticket_id).status is TicketStatus.ESCALATED


def test_timeout_escalates_without_acting(tmp_path):
    mgr, tickets, incidents, _, runner = build(tmp_path)
    inc = diagnosing_incident(tickets, incidents)
    mgr.request_approval(inc, approval_dx(), now=at(10))
    # before the deadline: nothing happens
    mgr.check_approval_timeouts(now=at(100))
    assert incidents.get("INC-1").state is IncidentState.AWAITING_APPROVAL
    # past the 15-min deadline: escalate, no action
    mgr.check_approval_timeouts(now=at(10 + 901))
    assert incidents.get("INC-1").state is IncidentState.ESCALATED
    assert runner.calls == []


def test_step_routes_approval_tier_to_request(tmp_path):
    """_progress (used by step) must request approval for a Tier-2 action, not escalate it."""
    mgr, tickets, incidents, notifier, runner = build(tmp_path)
    inc = diagnosing_incident(tickets, incidents)
    # drive _progress directly with an injected diagnoser returning an APPROVAL action
    mgr._diagnoser = _StubDiagnoser(approval_dx())
    mgr._assembler = _StubAssembler()
    mgr._progress(inc, window=None, signals=None, now=at(10))
    assert incidents.get("INC-1").state is IncidentState.AWAITING_APPROVAL
    assert runner.calls == []


def test_manual_resolve_closes_an_escalated_incident(tmp_path):
    # the human fix loop: reject a Tier-2 → ESCALATED, operator fixes it out-of-band, then
    # closes the ticket from the UI/CLI. The agent never auto-closes a handed-off incident,
    # so this is the only path that resolves it.
    mgr, tickets, incidents, notifier, runner = build(tmp_path)
    inc = diagnosing_incident(tickets, incidents)
    mgr.request_approval(inc, approval_dx(), now=at(10))
    mgr.reject("INC-1", approver="bob@example.com", now=at(60))
    assert incidents.get("INC-1").state is IncidentState.ESCALATED

    closed = mgr.manual_resolve("INC-1", resolver="bob@example.com", now=at(120))
    assert closed is not None
    stored = incidents.get("INC-1")
    assert stored.state is IncidentState.RESOLVED
    assert stored.resolved_at is not None
    assert tickets.get(inc.ticket_id).status is TicketStatus.RESOLVED
    comments = " ".join(c.body for c in tickets.get(inc.ticket_id).comments)
    assert "manually resolved by bob@example.com" in comments
    assert runner.calls == []                          # closure runs no remediation
    assert notifier.sent[-1].kind == "resolved"


def test_manual_resolve_closes_a_flapping_incident(tmp_path):
    mgr, tickets, incidents, _, _ = build(tmp_path)
    ticket = tickets.create(fingerprint="redis:unreachable", title="x", services=["worker"],
                            fault_type="unreachable", severity="High", evidence="x", now=T0)
    inc = Incident(id="INC-7", fingerprint="redis:unreachable", ticket_id=ticket.id,
                   state=IncidentState.FLAPPING, root_service="redis", services=["worker"],
                   fault_type="unreachable", severity="High", first_seen=T0, updated_at=T0)
    incidents.save(inc)
    assert mgr.manual_resolve("INC-7", resolver="carol", now=at(30)) is not None
    assert incidents.get("INC-7").state is IncidentState.RESOLVED


def test_manual_resolve_is_a_noop_on_agent_owned_states(tmp_path):
    # an incident the agent is still working (awaiting approval / verifying) must NOT be
    # closeable by the manual path — that would race the agent's own lifecycle.
    mgr, tickets, incidents, _, runner = build(tmp_path)
    inc = diagnosing_incident(tickets, incidents)
    mgr.request_approval(inc, approval_dx(), now=at(10))   # → AWAITING_APPROVAL
    assert mgr.manual_resolve("INC-1", resolver="eve", now=at(20)) is None
    assert incidents.get("INC-1").state is IncidentState.AWAITING_APPROVAL
    assert runner.calls == []


def test_manual_resolve_unknown_incident_returns_none(tmp_path):
    mgr, *_ = build(tmp_path)
    assert mgr.manual_resolve("INC-404", resolver="eve", now=at(20)) is None


class _StubAssembler:
    def build(self, incident, window, now, signals=None):
        return "sys", "user"

    def make_verifier(self, corpus):
        return lambda ref: True


class _StubDiagnoser:
    def __init__(self, dx):
        self._dx = dx

    def diagnose(self, *, system, user, verify_evidence, restart_target=None):
        return self._dx
