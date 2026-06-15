"""Integration tests for the M5 exit criteria: a Tier-2 fault runs end-to-end with one
human approval, and an un-approved Tier-2 proposal escalates on timeout with NO action
taken. Real engine + manager + executor; a fake LLM proposes an APPROVAL-tier action."""
import json
from datetime import timedelta

from sre_agent.action.backend import DockerActionBackend
from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.changelog import ChangeLog
from sre_agent.diagnosis.context import ContextAssembler
from sre_agent.diagnosis.diagnoser import Diagnoser
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.ticketing import TicketStatus
from tests.integration.harness import PipelineHarness, T0, integration_config

ALL = ["gateway", "webapp", "api", "worker", "loadgen"]

# Tier-2 proposal: restart redis (APPROVAL). Evidence cites a grounded prompt token.
RESTART_REDIS = json.dumps({
    "root_cause": "redis is unreachable; restarting it should restore the worker",
    "evidence": ["the incident header shows root_service=redis"],
    "selected_action": "restart_container",
    "action_params": {"service": "redis"},
    "alternative_hypotheses": [],
})


class FakeProvider:
    def __init__(self, response):
        self._response = response

    def complete(self, *, system, user, temperature=0.0):
        return self._response


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return 0, "ok", ""


def build(tmp_path):
    cfg = integration_config()
    changelog = ChangeLog(tmp_path / "c.db")
    runner = FakeRunner()
    h = PipelineHarness(
        tmp_path, cfg,
        diagnoser=Diagnoser(FakeProvider(RESTART_REDIS), runs=1),
        assembler=ContextAssembler(LAB_TOPOLOGY, cfg),
        executor=ActionExecutor(backend=DockerActionBackend(runner=runner), dry_run=False),
        guardrails=Guardrails(),
        recovery=RecoveryEvaluator(cfg),
        changelog=changelog,
    )
    return h, runner


def emit(h, t, *, redis_errors: bool):
    for svc in ALL:
        if svc == "worker" and redis_errors:
            h.emit("worker", t, level="ERROR", event="redis_down", request_id=f"w{int(t)}",
                   error="redis_unreachable")
        else:
            h.emit(svc, t, level="INFO", event="request", status=200, request_id=f"ok{int(t)}")


def test_tier2_waits_for_approval_then_remediates(tmp_path):
    h, runner = build(tmp_path)
    # redis errors until the human approves the restart; then clean (the action "fixed" it)
    t = 0
    approved = False
    while t <= 140:
        emit(h, t, redis_errors=(not approved))
        h.advance_full(t)
        inc = h.incidents.find_by_state(IncidentState.AWAITING_APPROVAL)
        if inc and not approved:
            assert runner.calls == []                     # nothing ran before approval
            h.manager.approve(inc[0].id, approver="alice@example.com",
                              now=T0 + timedelta(seconds=t))
            approved = True
        t += 2
    assert approved, "expected an approval request"
    assert runner.calls == [["docker", "restart", "redis"]]   # ran exactly once, after approval
    resolved = h.incidents.find_by_state(IncidentState.RESOLVED)
    assert len(resolved) == 1
    assert h.tickets.get(resolved[0].ticket_id).status is TicketStatus.RESOLVED


def test_tier2_timeout_escalates_without_action(tmp_path):
    h, runner = build(tmp_path)
    t = 0
    while t <= 120:                # never approve; redis errors persist
        emit(h, t, redis_errors=True)
        h.advance_full(t)
        t += 2
    escalated = h.incidents.find_by_state(IncidentState.ESCALATED)
    assert len(escalated) == 1
    assert runner.calls == []                              # timed out → no action taken
    assert h.tickets.get(escalated[0].ticket_id).status is TicketStatus.ESCALATED
