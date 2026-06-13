"""TDD: the Incident Manager — the M2 spine. Buffers candidates over the correlation
window, collapses a cascade into one incident, creates exactly one ticket per fault,
dedups recurrences into comments, reopens flapping incidents, and stays silent in
maintenance mode."""
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.config import Config
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.notifications import InMemoryNotifier
from sre_agent.integrations.ticketing import SqliteTicketStore, TicketStatus
from sre_agent.models import IncidentCandidate

T0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def cand(service, signal, codes=None, at_s=0.0):
    return IncidentCandidate(
        services=[service], signal_type=signal, detail=f"{service} {signal}",
        first_seen=T0, confirmed_at=T0 + timedelta(seconds=at_s), error_codes=codes or [],
    )


def cfg():
    c = Config()
    c.correlation_window_s = 5.0
    c.flap_window_s = 100.0
    c.flap_escalate_after = 3
    return c


@pytest.fixture
def ctx(tmp_path):
    config = cfg()
    tickets = SqliteTicketStore(tmp_path / "t.db")
    incidents = IncidentStore(tmp_path / "i.db")
    notifier = InMemoryNotifier()
    mgr = IncidentManager(incidents, tickets, notifier, LAB_TOPOLOGY, config)
    return mgr, tickets, incidents, notifier, config


def mature(now_s):
    """A time comfortably past the correlation window for a candidate ingested at t=0."""
    return T0 + timedelta(seconds=now_s)


# --- correlation window buffering -----------------------------------------------
def test_does_not_ticket_before_correlation_window(ctx):
    mgr, tickets, *_ = ctx
    mgr.ingest(cand("worker", "silence"), now=T0)
    assert mgr.tick(now=T0 + timedelta(seconds=2)) == []   # still collecting
    assert tickets.list_open() == []


def test_creates_one_incident_after_window(ctx):
    mgr, tickets, incidents, notifier, _ = ctx
    mgr.ingest(cand("worker", "silence"), now=T0)
    created = mgr.tick(now=mature(10))
    assert len(created) == 1
    inc = created[0]
    assert inc.state is IncidentState.DETECTED
    assert inc.fingerprint == "worker:silence"
    assert len(tickets.list_open()) == 1
    assert tickets.get(inc.ticket_id).fingerprint == "worker:silence"
    assert [n.kind for n in notifier.sent] == ["created"]


# --- one fault = one ticket (correlation) ---------------------------------------
def test_cascade_collapses_to_single_ticket(ctx):
    mgr, tickets, *_ = ctx
    mgr.ingest(cand("api", "error_rate", codes=["redis_unreachable"]), now=T0)
    mgr.ingest(cand("worker", "silence", at_s=1), now=T0 + timedelta(seconds=1))
    created = mgr.tick(now=mature(10))
    assert len(created) == 1
    assert created[0].fingerprint == "redis:unreachable"
    assert set(created[0].services) == {"api", "worker"}
    assert len(tickets.list_open()) == 1   # NOT two


# --- dedup ----------------------------------------------------------------------
def test_recurrence_comments_not_duplicates(ctx):
    mgr, tickets, incidents, notifier, _ = ctx
    mgr.ingest(cand("worker", "silence"), now=T0)
    first = mgr.tick(now=mature(10))[0]
    # same fault fires again later, still unresolved
    mgr.ingest(cand("worker", "silence", at_s=30), now=mature(30))
    again = mgr.tick(now=mature(40))
    assert len(tickets.list_open()) == 1            # no duplicate
    assert again[0].id == first.id
    comments = tickets.get(first.ticket_id).comments
    assert any("recur" in c.body.lower() for c in comments)
    assert [n.kind for n in notifier.sent] == ["created"]  # no second alert


# --- maintenance mode -----------------------------------------------------------
def test_maintenance_mode_is_silent(ctx):
    mgr, tickets, incidents, notifier, config = ctx
    config.maintenance_mode = True
    mgr.ingest(cand("api", "error_rate", codes=["redis_unreachable"]), now=T0)
    assert mgr.tick(now=mature(10)) == []
    assert tickets.list_open() == []
    assert notifier.sent == []


# --- flapping / reopen ----------------------------------------------------------
def test_recurrence_after_resolution_reopens_within_flap_window(ctx):
    mgr, tickets, incidents, notifier, _ = ctx
    mgr.ingest(cand("worker", "silence"), now=T0)
    inc = mgr.tick(now=mature(10))[0]
    mgr.resolve(inc.id, now=mature(20))            # incident resolved
    assert tickets.get(inc.ticket_id).status is TicketStatus.RESOLVED

    # recurs 30s later — within the 100s flap window
    mgr.ingest(cand("worker", "silence", at_s=50), now=mature(50))
    reopened = mgr.tick(now=mature(60))
    assert len(tickets.list_open()) == 1           # reused, not new
    assert reopened[0].id == inc.id
    assert reopened[0].state is IncidentState.FLAPPING
    assert reopened[0].flap_count == 1
    assert "reopened" in tickets.get(inc.ticket_id).status.value.lower() or \
        tickets.get(inc.ticket_id).status is TicketStatus.OPEN
    assert notifier.sent[-1].kind == "reopened"


def test_recurrence_after_flap_window_is_a_new_ticket(ctx):
    mgr, tickets, incidents, _, _ = ctx
    mgr.ingest(cand("worker", "silence"), now=T0)
    inc = mgr.tick(now=mature(10))[0]
    mgr.resolve(inc.id, now=mature(20))
    # recurs well after the 100s flap window
    mgr.ingest(cand("worker", "silence", at_s=500), now=mature(500))
    again = mgr.tick(now=mature(510))
    assert again[0].id != inc.id                   # brand new incident
    assert len({t.id for t in [tickets.get(inc.ticket_id), tickets.get(again[0].ticket_id)]}) == 2


def test_repeated_flapping_escalates(ctx):
    mgr, tickets, incidents, notifier, config = ctx
    mgr.ingest(cand("worker", "silence"), now=T0)
    inc = mgr.tick(now=mature(10))[0]
    t = 20
    for _ in range(config.flap_escalate_after):
        mgr.resolve(inc.id, now=mature(t))
        mgr.ingest(cand("worker", "silence", at_s=t + 5), now=mature(t + 5))
        result = mgr.tick(now=mature(t + 10))[0]
        t += 30
    assert result.state is IncidentState.ESCALATED
    assert tickets.get(inc.ticket_id).status is TicketStatus.ESCALATED
    assert tickets.get(inc.ticket_id).assignee is not None


# --- severity mapping -----------------------------------------------------------
def test_severity_high_when_user_facing(ctx):
    mgr, tickets, *_ = ctx
    mgr.ingest(cand("loadgen", "error_rate", codes=["internal_error"]), now=T0)
    inc = mgr.tick(now=mature(10))[0]
    assert tickets.get(inc.ticket_id).severity == "High"


def test_severity_medium_for_background_lag(ctx):
    mgr, tickets, *_ = ctx
    mgr.ingest(cand("worker", "silence"), now=T0)
    inc = mgr.tick(now=mature(10))[0]
    assert tickets.get(inc.ticket_id).severity == "Medium"


# --- restart safety -------------------------------------------------------------
def test_dedup_survives_manager_restart(tmp_path):
    config = cfg()
    tpath, ipath = tmp_path / "t.db", tmp_path / "i.db"
    notifier = InMemoryNotifier()
    mgr1 = IncidentManager(IncidentStore(ipath), SqliteTicketStore(tpath), notifier,
                           LAB_TOPOLOGY, config)
    mgr1.ingest(cand("worker", "silence"), now=T0)
    inc = mgr1.tick(now=mature(10))[0]

    # fresh manager on the same DBs = restart
    tickets2 = SqliteTicketStore(tpath)
    mgr2 = IncidentManager(IncidentStore(ipath), tickets2, InMemoryNotifier(),
                           LAB_TOPOLOGY, config)
    mgr2.ingest(cand("worker", "silence", at_s=30), now=mature(30))
    mgr2.tick(now=mature(40))
    assert len(tickets2.list_open()) == 1          # still deduped after restart
    assert tickets2.get(inc.ticket_id).comments    # commented the recurrence
