"""TDD: the manager writes a post-mortem when an incident resolves or escalates, capturing
the diagnosed root cause and the action taken — the raw material for incident memory (M6)."""
import json
from datetime import timedelta

from sre_agent.action.catalog import Tier
from sre_agent.action.backend import DockerActionBackend
from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.changelog import ChangeLog
from sre_agent.config import Config
from sre_agent.diagnosis.context import ContextAssembler
from sre_agent.diagnosis.diagnoser import Diagnoser
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.notifications import InMemoryNotifier
from sre_agent.integrations.postmortems import SqlitePostMortemStore
from sre_agent.integrations.ticketing import SqliteTicketStore
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, rec

DX = ('{"root_cause":"worker hung and stopped processing","evidence":["root_service=worker"],'
      '"selected_action":"restart_container","action_params":{"service":"worker"},'
      '"alternative_hypotheses":[]}')


class FakeProvider:
    def __init__(self, response):
        self.response = response

    def complete(self, *, system, user, temperature=0.0):
        return self.response


class Runner:
    def __call__(self, cmd):
        return 0, "ok", ""


def build(tmp_path, response=DX):
    cfg = Config()
    cfg.silence_threshold_s = 30.0
    tickets = SqliteTicketStore(tmp_path / "t.db")
    incidents = IncidentStore(tmp_path / "i.db")
    changelog = ChangeLog(tmp_path / "c.db")
    pms = SqlitePostMortemStore(tmp_path / "pm.db", markdown_dir=tmp_path / "pms")
    mgr = IncidentManager(incidents, tickets, InMemoryNotifier(), LAB_TOPOLOGY, cfg,
                          diagnoser=Diagnoser(FakeProvider(response), runs=1),
                          assembler=ContextAssembler(LAB_TOPOLOGY, cfg),
                          executor=ActionExecutor(backend=DockerActionBackend(runner=Runner()), dry_run=False),
                          guardrails=Guardrails(),
                          recovery=RecoveryEvaluator(cfg), changelog=changelog, postmortems=pms)
    return mgr, incidents, pms


def detected(incidents, tickets):
    t = tickets.create(fingerprint="worker:silence", title="x", services=["worker"],
                       fault_type="silence", severity="Medium", evidence="x", now=T0)
    inc = Incident(id="INC-1", fingerprint="worker:silence", ticket_id=t.id,
                   state=IncidentState.DETECTED, root_service="worker", services=["worker"],
                   fault_type="silence", severity="Medium", first_seen=T0, updated_at=T0)
    incidents.save(inc)
    return inc


def test_resolved_incident_writes_postmortem(tmp_path):
    mgr, incidents, pms = build(tmp_path)
    inc = detected(incidents, mgr._tickets)
    mgr._progress(inc, SlidingWindow(), None, now=T0 + timedelta(seconds=5))   # diagnose + act
    recovered = SlidingWindow()
    recovered.append(rec("worker", 60))
    mgr.verify_incident(incidents.get("INC-1"), recovered, None, now=T0 + timedelta(seconds=70))

    found = pms.recent_for_fingerprint("worker:silence")
    assert len(found) == 1
    assert found[0].outcome == "resolved"
    assert "hung" in found[0].root_cause
    assert "restart_container" in found[0].action_taken
    assert found[0].time_to_resolve_s is not None


def test_escalated_incident_writes_postmortem(tmp_path):
    # an invalid action escalates → still recorded (memory of a fault we couldn't auto-fix)
    bad = json.dumps({"root_cause": "mystery", "evidence": ["root_service=worker"],
                      "selected_action": "rm_rf", "action_params": {}, "alternative_hypotheses": []})
    mgr, incidents, pms = build(tmp_path, response=bad)
    inc = detected(incidents, mgr._tickets)
    mgr._progress(inc, SlidingWindow(), None, now=T0 + timedelta(seconds=5))
    assert incidents.get("INC-1").state is IncidentState.ESCALATED
    found = pms.recent_for_fingerprint("worker:silence")
    assert len(found) == 1 and found[0].outcome == "escalated"
