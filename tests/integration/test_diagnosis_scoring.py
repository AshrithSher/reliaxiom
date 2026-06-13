"""Offline integration test for the diagnosis-accuracy scoring and the escalation path.
Uses a fake provider so it's fast and deterministic; the live ≥4/5 number comes from
eval/diagnosis_eval.run_live against real Gemini."""
import json
from datetime import datetime, timedelta, timezone

from eval.diagnosis_eval import CASES, DiagnosisCase, score
from sre_agent.config import Config
from sre_agent.diagnosis.context import ContextAssembler
from sre_agent.diagnosis.diagnoser import Diagnoser
from sre_agent.diagnosis.schema import Diagnosis
from sre_agent.action.catalog import Tier
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import LogRecord

T0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def dx(root="redis maxmemory exhausted", action="restart_container", escalate=False):
    return Diagnosis(root_cause=root, evidence=[], selected_action=action, action_params={},
                     alternatives=[], tier=Tier.ESCALATE if escalate else Tier.AUTO,
                     escalate=escalate, escalation_reasons=[])


def case(name="kill-redis"):
    return next(c for c in CASES if c.name == name)


# --- score() --------------------------------------------------------------------
def test_score_matches_expected_keyword():
    assert score(dx(root="The redis cache is unreachable"), case("kill-redis"))


def test_score_fails_on_wrong_root():
    assert not score(dx(root="the gateway is slow"), case("kill-redis"))


def test_score_escalation_case_passes_when_escalated():
    assert score(dx(escalate=True), case("ungroundable"))


def test_score_escalation_case_fails_when_confident():
    assert not score(dx(escalate=False), case("ungroundable"))


def test_score_counts_action_target_too():
    # root_cause vague but action names the component → still correct
    assert score(dx(root="dependency failure", action="restart_container"), case("kill-worker")) is False
    d = dx(root="the worker process is down", action="restart_container")
    assert score(d, case("kill-worker"))


# --- pipeline: ungroundable incident over healthy logs must escalate -------------
class FakeProvider:
    def __init__(self, response):
        self._response = response

    def complete(self, *, system, user, temperature=0.0):
        return self._response


def test_ungroundable_fault_escalates_through_real_pipeline():
    # healthy window — no errors. Model fabricates an error citation → guard trips.
    window = SlidingWindow()
    window.append(LogRecord(ts=T0, service="api", level="INFO", event="request",
                            status=200, request_id="real-1", raw={"service": "api"}))
    inc = Incident(id="INC-x", fingerprint="api:phantom", ticket_id=None,
                   state=IncidentState.DETECTED, root_service="api", services=["api"],
                   fault_type="phantom", severity="Low", first_seen=T0, updated_at=T0)
    response = json.dumps({
        "root_cause": "api crashed", "evidence": ["request fab-9999 returned 500"],
        "selected_action": "restart_container", "action_params": {"service": "api"},
        "alternative_hypotheses": [],
    })
    a = ContextAssembler(LAB_TOPOLOGY, Config())
    system, user = a.build(inc, window, T0 + timedelta(seconds=5))
    verifier = a.make_verifier(a.evidence_corpus(inc, window, T0 + timedelta(seconds=5)))
    d = Diagnoser(FakeProvider(response), runs=2).diagnose(system=system, user=user,
                                                           verify_evidence=verifier)
    assert d.escalate   # cited "fab-9999" is not in the (healthy) logs → Tier 3
    assert score(d, case("ungroundable"))
