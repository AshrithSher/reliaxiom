"""Integration test for the M6 exit criterion: the SECOND occurrence of a known fault is
diagnosed with the prior post-mortem in its prompt (incident memory). Spans manager +
assembler + post-mortem store; a capturing provider lets us inspect the actual prompt."""
from datetime import timedelta

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

DX = ('{"root_cause":"worker hung and stopped processing jobs","evidence":["root_service=worker"],'
      '"selected_action":"restart_container","action_params":{"service":"worker"},'
      '"alternative_hypotheses":[]}')


class CapturingProvider:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def complete(self, *, system, user, temperature=0.0):
        self.prompts.append(user)
        return self.response


class Runner:
    def __call__(self, cmd):
        return 0, "ok", ""


def silence_incident(idx, incidents, tickets):
    t = tickets.create(fingerprint="worker:silence", title="x", services=["worker"],
                       fault_type="silence", severity="Medium", evidence="x", now=T0)
    inc = Incident(id=idx, fingerprint="worker:silence", ticket_id=t.id,
                   state=IncidentState.DETECTED, root_service="worker", services=["worker"],
                   fault_type="silence", severity="Medium", first_seen=T0, updated_at=T0)
    incidents.save(inc)
    return inc


def recovered_window():
    w = SlidingWindow()
    w.append(rec("worker", 60))
    return w


def test_second_occurrence_is_diagnosed_with_prior_postmortem(tmp_path):
    cfg = Config()
    cfg.silence_threshold_s = 30.0
    tickets = SqliteTicketStore(tmp_path / "t.db")
    incidents = IncidentStore(tmp_path / "i.db")
    changelog = ChangeLog(tmp_path / "c.db")
    pms = SqlitePostMortemStore(tmp_path / "pm.db")
    provider = CapturingProvider(DX)
    mgr = IncidentManager(incidents, tickets, InMemoryNotifier(), LAB_TOPOLOGY, cfg,
                          diagnoser=Diagnoser(provider, runs=1),
                          assembler=ContextAssembler(LAB_TOPOLOGY, cfg, postmortems=pms),
                          executor=ActionExecutor(runner=Runner(), dry_run=False),
                          guardrails=Guardrails(cfg.max_restarts_per_hour, changelog),
                          recovery=RecoveryEvaluator(cfg), changelog=changelog, postmortems=pms)

    # --- first occurrence: diagnose → act → resolve (writes a post-mortem) ---
    inc1 = silence_incident("INC-1", incidents, tickets)
    mgr._progress(inc1, SlidingWindow(), None, now=T0 + timedelta(seconds=5))
    mgr.verify_incident(incidents.get("INC-1"), recovered_window(), None,
                        now=T0 + timedelta(seconds=70))
    assert incidents.get("INC-1").state is IncidentState.RESOLVED
    assert "INCIDENT MEMORY" not in provider.prompts[0]   # nothing known the first time

    # --- second occurrence of the same fingerprint ---
    inc2 = silence_incident("INC-2", incidents, tickets)
    mgr._progress(inc2, SlidingWindow(), None, now=T0 + timedelta(seconds=200))

    second_prompt = provider.prompts[-1]
    assert "INCIDENT MEMORY" in second_prompt                   # memory was injected
    assert "worker hung and stopped processing" in second_prompt   # the prior root cause
    assert "restart_container" in second_prompt                # and the fix that worked
