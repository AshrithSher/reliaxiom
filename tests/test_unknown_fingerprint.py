"""TDD: the optional unknown-fingerprint policy (D-021). When enabled, a fault with no prior
post-mortem is held for a human; once a post-mortem exists (it's been seen), the agent
auto-acts. Off by default, so first-occurrence auto-remediation is preserved."""
from datetime import datetime, timedelta, timezone

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
from sre_agent.integrations.postmortems import PostMortem, SqlitePostMortemStore
from sre_agent.integrations.ticketing import SqliteTicketStore
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0

DX = ('{"root_cause":"worker hung","evidence":["root_service=worker"],'
      '"selected_action":"restart_container","action_params":{"service":"worker"},'
      '"alternative_hypotheses":[]}')


class Provider:
    def complete(self, *, system, user, temperature=0.0):
        return DX


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return 0, "ok", ""


def build(tmp_path, escalate_unknown):
    cfg = Config()
    cfg.escalate_unknown_fingerprint = escalate_unknown
    tickets = SqliteTicketStore(tmp_path / "t.db")
    incidents = IncidentStore(tmp_path / "i.db")
    changelog = ChangeLog(tmp_path / "c.db")
    pms = SqlitePostMortemStore(tmp_path / "pm.db")
    runner = Runner()
    mgr = IncidentManager(incidents, tickets, InMemoryNotifier(), LAB_TOPOLOGY, cfg,
                          diagnoser=Diagnoser(Provider(), runs=1),
                          assembler=ContextAssembler(LAB_TOPOLOGY, cfg, postmortems=pms),
                          executor=ActionExecutor(runner=runner, dry_run=False),
                          guardrails=Guardrails(cfg.max_restarts_per_hour, changelog),
                          recovery=RecoveryEvaluator(cfg), changelog=changelog, postmortems=pms)
    return mgr, incidents, pms, runner


def incident(incidents, tickets):
    t = tickets.create(fingerprint="worker:silence", title="x", services=["worker"],
                       fault_type="silence", severity="Medium", evidence="x", now=T0)
    inc = Incident(id="INC-1", fingerprint="worker:silence", ticket_id=t.id,
                   state=IncidentState.DETECTED, root_service="worker", services=["worker"],
                   fault_type="silence", severity="Medium", first_seen=T0, updated_at=T0)
    incidents.save(inc)
    return inc


def test_default_off_acts_on_first_occurrence(tmp_path):
    mgr, incidents, _, runner = build(tmp_path, escalate_unknown=False)
    mgr._progress(incident(incidents, mgr._tickets), SlidingWindow(), None, T0 + timedelta(seconds=5))
    assert runner.calls == [["docker", "restart", "worker"]]   # auto-remediated


def test_enabled_holds_novel_fault_for_human(tmp_path):
    mgr, incidents, _, runner = build(tmp_path, escalate_unknown=True)
    mgr._progress(incident(incidents, mgr._tickets), SlidingWindow(), None, T0 + timedelta(seconds=5))
    assert runner.calls == []                                  # not acted
    assert incidents.get("INC-1").state is IncidentState.ESCALATED


def test_enabled_acts_when_fingerprint_is_known(tmp_path):
    mgr, incidents, pms, runner = build(tmp_path, escalate_unknown=True)
    pms.record(PostMortem(incident_id="OLD", ticket_id="SRE-0", fingerprint="worker:silence",
                          root_service="worker", services=["worker"], root_cause="hung",
                          action_taken="restart_container worker", outcome="resolved",
                          time_to_resolve_s=60.0,
                          created_at=datetime(2026, 6, 11, tzinfo=timezone.utc)))
    mgr._progress(incident(incidents, mgr._tickets), SlidingWindow(), None, T0 + timedelta(seconds=5))
    assert runner.calls == [["docker", "restart", "worker"]]   # known fault → auto-acts
