"""Integration test for the M4 exit criterion: a fault is detected, diagnosed, auto-
remediated, and the ticket auto-resolves on verified recovery — AND the agent does not
raise a second incident from its own restart (invariant #5). Real engine + manager +
executor (fake runner) + recovery; a fake LLM stands in for diagnosis."""
import json

from sre_agent.action.backend import DockerActionBackend
from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.changelog import ChangeLog
from sre_agent.diagnosis.context import ContextAssembler
from sre_agent.diagnosis.diagnoser import Diagnoser
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.ticketing import TicketStatus
from tests.integration.harness import PipelineHarness, integration_config

ALL = ["gateway", "webapp", "api", "worker", "loadgen"]


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
        return 0, "restarted", ""


RESTART_WORKER = json.dumps({
    "root_cause": "the worker has hung and stopped processing",
    # cite a significant token actually in the prompt (the incident header has root_service)
    "evidence": ["the incident header shows root_service=worker with fault=silence"],
    "selected_action": "restart_container",
    "action_params": {"service": "worker"},
    "alternative_hypotheses": [],
})


def build(tmp_path):
    cfg = integration_config()
    changelog = ChangeLog(tmp_path / "c.db")
    runner = FakeRunner()
    h = PipelineHarness(
        tmp_path, cfg,
        diagnoser=Diagnoser(FakeProvider(RESTART_WORKER), runs=2),
        assembler=ContextAssembler(LAB_TOPOLOGY, cfg),
        executor=ActionExecutor(backend=DockerActionBackend(runner=runner), dry_run=False),
        guardrails=Guardrails(),
        recovery=RecoveryEvaluator(cfg),
        changelog=changelog,
    )
    return h, runner, changelog


def test_kill_worker_auto_remediated_and_resolved(tmp_path):
    h, runner, changelog = build(tmp_path)
    t = 0
    while t <= 90:
        for svc in ALL:
            # worker goes silent from t=20, then "comes back" from t=58 (post-restart)
            if svc == "worker" and 20 < t < 58:
                continue
            h.emit(svc, t, level="INFO", event="request", status=200, request_id=f"ok{int(t)}")
        h.advance_full(t)
        t += 2

    incidents = h.incidents.find_by_state(IncidentState.RESOLVED)
    assert len(incidents) == 1                       # exactly one incident, now resolved
    inc = incidents[0]
    assert inc.fingerprint == "worker:silence"
    assert h.tickets.get(inc.ticket_id).status is TicketStatus.RESOLVED
    # the agent actually executed the restart, and tagged it as its own action
    assert runner.calls == [["docker", "restart", "worker"]]
    tagged = [e for e in changelog.recent(h_T0(-1), h_T0(200))
              if e.actor == "sre-agent" and e.service == "worker"]
    assert len(tagged) == 1
    # no duplicate / self-induced incident was ever opened
    assert h.tickets.get("SRE-2") is None


def test_failed_restart_never_recovers_and_escalates(tmp_path):
    # runner "succeeds" but the worker never logs again → recovery never holds → escalate
    h, runner, changelog = build(tmp_path)
    t = 0
    while t <= 140:
        for svc in ALL:
            if svc == "worker" and t > 20:
                continue                              # worker stays dead the whole time
            h.emit(svc, t, level="INFO", event="request", status=200, request_id=f"ok{int(t)}")
        h.advance_full(t)
        t += 2
    escalated = h.incidents.find_by_state(IncidentState.ESCALATED)
    assert len(escalated) == 1
    assert h.tickets.get(escalated[0].ticket_id).status is TicketStatus.ESCALATED


def h_T0(offset_s):
    from datetime import timedelta
    from tests.integration.harness import T0
    return T0 + timedelta(seconds=offset_s)
