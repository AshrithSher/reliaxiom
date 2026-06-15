"""TDD: a transient LLM outage (429/timeout) must NOT permanently escalate. The agent retries
on later cycles and self-heals when the provider returns; only after a cap does it give up to
a human. A *content* failure (bad JSON) still escalates immediately — that won't fix itself."""
from datetime import timedelta

from sre_agent.action.catalog import Tier
from sre_agent.action.backend import DockerActionBackend
from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.changelog import ChangeLog
from sre_agent.config import Config
from sre_agent.diagnosis.diagnoser import Diagnoser
from sre_agent.diagnosis.llm import LLMError
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.notifications import InMemoryNotifier
from sre_agent.integrations.ticketing import SqliteTicketStore
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, rec

WORKER_FIX = ('{"root_cause":"worker hung","evidence":["root_service=worker"],'
              '"selected_action":"restart_container","action_params":{"service":"worker"},'
              '"alternative_hypotheses":[]}')


class FlakyProvider:
    """Raises (transport down) for the first `fail_times` calls, then returns a good response."""

    def __init__(self, fail_times, response=WORKER_FIX):
        self.fail_times = fail_times
        self.response = response
        self.calls = 0

    def complete(self, *, system, user, temperature=0.0):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMError("HTTP Error 429: Too Many Requests")
        return self.response


# --- diagnoser flags transient vs content failure --------------------------------
def test_diagnoser_marks_transport_failure_unavailable():
    out = Diagnoser(FlakyProvider(fail_times=1), runs=1).diagnose(
        system="s", user="u", verify_evidence=lambda r: True)
    assert out.provider_unavailable and out.escalate


def test_diagnoser_bad_json_is_not_unavailable():
    out = Diagnoser(_Const("not json"), runs=1).diagnose(
        system="s", user="u", verify_evidence=lambda r: True)
    assert out.escalate and not out.provider_unavailable


class _Const:
    def __init__(self, text):
        self.text = text

    def complete(self, *, system, user, temperature=0.0):
        return self.text


# --- manager retries, then self-heals --------------------------------------------
def _mgr(tmp_path, provider, runner):
    cfg = Config()
    cfg.max_diagnosis_attempts = 3
    cfg.silence_threshold_s = 30.0
    tickets = SqliteTicketStore(tmp_path / "t.db")
    incidents = IncidentStore(tmp_path / "i.db")
    changelog = ChangeLog(tmp_path / "c.db")
    from sre_agent.diagnosis.context import ContextAssembler
    mgr = IncidentManager(incidents, tickets, InMemoryNotifier(), LAB_TOPOLOGY, cfg,
                          diagnoser=Diagnoser(provider, runs=1),
                          assembler=ContextAssembler(LAB_TOPOLOGY, cfg),
                          executor=ActionExecutor(backend=DockerActionBackend(runner=runner), dry_run=False),
                          guardrails=Guardrails(),
                          recovery=RecoveryEvaluator(cfg), changelog=changelog)
    return mgr, incidents


def _incident(incidents, tickets):
    t = tickets.create(fingerprint="worker:silence", title="x", services=["worker"],
                       fault_type="silence", severity="Medium", evidence="x", now=T0)
    inc = Incident(id="INC-1", fingerprint="worker:silence", ticket_id=t.id,
                   state=IncidentState.DETECTED, root_service="worker", services=["worker"],
                   fault_type="silence", severity="Medium", first_seen=T0, updated_at=T0)
    incidents.save(inc)
    return inc


class _Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return 0, "ok", ""


def window_with_worker(at_s):
    w = SlidingWindow()
    w.append(rec("worker", at_s))
    return w


def test_transient_failure_stays_diagnosing_then_acts_on_recovery(tmp_path):
    runner = _Runner()
    provider = FlakyProvider(fail_times=2)              # 2 outages, then good
    mgr, incidents = _mgr(tmp_path, provider, runner)
    tickets = mgr._tickets
    inc = _incident(incidents, tickets)
    empty = SlidingWindow()

    # cycle 1: LLM down → deferred, still DIAGNOSING, no action
    mgr._progress(inc, empty, None, now=T0 + timedelta(seconds=5))
    assert incidents.get("INC-1").state is IncidentState.DIAGNOSING
    assert incidents.get("INC-1").diagnosis_attempts == 1
    assert runner.calls == []

    # cycle 2: still down → deferred again
    mgr._progress(incidents.get("INC-1"), empty, None, now=T0 + timedelta(seconds=10))
    assert incidents.get("INC-1").diagnosis_attempts == 2

    # cycle 3: LLM back → diagnoses and acts (restart worker)
    mgr._progress(incidents.get("INC-1"), empty, None, now=T0 + timedelta(seconds=15))
    assert runner.calls == [["docker", "restart", "worker"]]
    assert incidents.get("INC-1").state is IncidentState.VERIFYING


def test_persistent_outage_escalates_after_cap(tmp_path):
    runner = _Runner()
    mgr, incidents = _mgr(tmp_path, FlakyProvider(fail_times=99), runner)   # never recovers
    inc = _incident(incidents, mgr._tickets)
    t = 5
    for _ in range(3):                                  # max_diagnosis_attempts = 3
        mgr._progress(incidents.get("INC-1"), SlidingWindow(), None, now=T0 + timedelta(seconds=t))
        t += 5
    assert incidents.get("INC-1").state is IncidentState.ESCALATED
    assert runner.calls == []
