"""TDD: the manager driving diagnosis. On a DETECTED incident it assembles context, runs the
diagnoser, comments the result, and either parks it in DIAGNOSING (a real action proposed) or
escalates (a trust guard tripped). Diagnoser/assembler are injected — without them the manager
behaves exactly as in M2."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.action.catalog import Tier
from sre_agent.config import Config
from sre_agent.diagnosis.context import ContextAssembler
from sre_agent.diagnosis.diagnoser import Diagnoser
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.notifications import InMemoryNotifier
from sre_agent.integrations.ticketing import SqliteTicketStore, TicketStatus
from sre_agent.models import LogRecord

T0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(self, response):
        self._response = response

    def complete(self, *, system, user, temperature=0.0):
        return self._response


def llm_response(action="restart_container", params=None, evidence=None):
    return json.dumps({
        "root_cause": "redis unreachable",
        "evidence": evidence if evidence is not None else ["rid-7"],
        "selected_action": action,
        "action_params": params if params is not None else {"service": "worker"},
        "alternative_hypotheses": [],
    })


def build(tmp_path, response):
    cfg = Config()
    tickets = SqliteTicketStore(tmp_path / "t.db")
    incidents = IncidentStore(tmp_path / "i.db")
    notifier = InMemoryNotifier()
    mgr = IncidentManager(incidents, tickets, notifier, LAB_TOPOLOGY, cfg,
                          diagnoser=Diagnoser(FakeProvider(response), runs=2),
                          assembler=ContextAssembler(LAB_TOPOLOGY, cfg))
    return mgr, tickets, incidents, notifier


def seeded_incident(tickets, incidents):
    ticket = tickets.create(fingerprint="redis:unreachable", title="redis", services=["api", "worker"],
                            fault_type="unreachable", severity="High", evidence="x", now=T0)
    inc = Incident(id="INC-1", fingerprint="redis:unreachable", ticket_id=ticket.id,
                   state=IncidentState.DETECTED, root_service="redis", services=["api", "worker"],
                   fault_type="unreachable", severity="High", first_seen=T0, updated_at=T0)
    incidents.save(inc)
    return inc


def window_with(rid):
    from sre_agent.ingest.window import SlidingWindow
    w = SlidingWindow()
    w.append(LogRecord(ts=T0 + timedelta(seconds=1), service="api", level="ERROR",
                       event="request_failed", status=503, request_id=rid,
                       raw={"service": "api", "request_id": rid, "error": "redis_unreachable"},
                       error="redis_unreachable" if False else None))
    return w


@pytest.fixture
def now():
    return T0 + timedelta(seconds=30)


def test_diagnosis_parks_in_diagnosing_with_comment(tmp_path, now):
    mgr, tickets, incidents, _ = build(tmp_path, llm_response(evidence=["rid-7"]))
    inc = seeded_incident(tickets, incidents)
    diagnosis = mgr.diagnose(inc, window_with("rid-7"), now)
    assert not diagnosis.escalate
    assert incidents.get("INC-1").state is IncidentState.DIAGNOSING
    assert any("diagnosis" in c.body.lower() for c in tickets.get(inc.ticket_id).comments)


def test_diagnosis_escalates_on_hallucinated_evidence(tmp_path, now):
    # the model cites a request id that is NOT in the window → guard trips
    mgr, tickets, incidents, notifier = build(tmp_path, llm_response(evidence=["rid-doesnotexist"]))
    inc = seeded_incident(tickets, incidents)
    diagnosis = mgr.diagnose(inc, window_with("rid-7"), now)
    assert diagnosis.escalate
    assert incidents.get("INC-1").state is IncidentState.ESCALATED
    assert tickets.get(inc.ticket_id).status is TicketStatus.ESCALATED
    assert tickets.get(inc.ticket_id).assignee is not None
    assert notifier.sent[-1].kind == "escalated"


def test_diagnosis_escalates_on_invalid_action(tmp_path, now):
    mgr, tickets, incidents, _ = build(tmp_path, llm_response(action="rm_minus_rf", params={}))
    inc = seeded_incident(tickets, incidents)
    diagnosis = mgr.diagnose(inc, window_with("rid-7"), now)
    assert diagnosis.escalate
    assert incidents.get("INC-1").state is IncidentState.ESCALATED


def test_diagnosis_pins_restart_to_root_service(tmp_path, now):
    # the model proposes restarting a stateless *symptom* service (worker), but the
    # deterministically-correlated root is the stateful redis. The restart target — and so
    # the tier — must follow the root, otherwise a Tier-2 stateful restart silently
    # downgrades to a Tier-1 auto-restart of the wrong service.
    mgr, tickets, incidents, _ = build(tmp_path, llm_response(action="restart_container",
                                                              params={"service": "worker"}))
    inc = seeded_incident(tickets, incidents)   # root_service="redis" (stateful)
    diagnosis = mgr.diagnose(inc, window_with("rid-7"), now)
    assert not diagnosis.escalate
    assert diagnosis.action_params["service"] == "redis"
    assert diagnosis.tier is Tier.APPROVAL


def test_no_diagnoser_is_noop(tmp_path, now):
    cfg = Config()
    tickets = SqliteTicketStore(tmp_path / "t.db")
    incidents = IncidentStore(tmp_path / "i.db")
    mgr = IncidentManager(incidents, tickets, InMemoryNotifier(), LAB_TOPOLOGY, cfg)
    inc = seeded_incident(tickets, incidents)
    assert mgr.diagnose(inc, window_with("rid-7"), now) is None
    assert incidents.get("INC-1").state is IncidentState.DETECTED   # unchanged
