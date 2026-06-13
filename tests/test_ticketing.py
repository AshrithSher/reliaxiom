"""TDD: ticketing interface + SQLite stub. This is the integration boundary — the same
shape JIRA will swap into later (sequential keys, status lifecycle, comments, assignee).
The store persists and dedup-queries; it does NOT itself enforce one-ticket-per-fault —
that's the Incident Manager's job using find_open_by_fingerprint.
"""
from datetime import datetime, timezone

import pytest

from sre_agent.integrations.ticketing import (
    SqliteTicketStore,
    TicketStatus,
)

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return SqliteTicketStore(tmp_path / "tickets.db")


def make(store, fingerprint="redis:oom", severity="High"):
    return store.create(
        fingerprint=fingerprint, title="Redis OOM", services=["redis", "api"],
        fault_type="oom", severity=severity, evidence="maxmemory exhausted", now=NOW,
    )


def test_create_assigns_sequential_keys(store):
    a = make(store)
    b = make(store, fingerprint="worker:silence")
    assert a.id == "SRE-1" and b.id == "SRE-2"
    assert a.status == TicketStatus.OPEN
    assert a.fingerprint == "redis:oom" and a.severity == "High"


def test_get_roundtrips_fields(store):
    created = make(store)
    got = store.get(created.id)
    assert got is not None
    assert got.services == ["redis", "api"]
    assert got.fault_type == "oom"
    assert got.evidence == "maxmemory exhausted"
    assert got.created_at == NOW


def test_find_open_by_fingerprint_for_dedup(store):
    a = make(store)
    assert store.find_open_by_fingerprint("redis:oom").id == a.id
    assert store.find_open_by_fingerprint("nothing:here") is None


def test_resolved_ticket_is_not_open(store):
    a = make(store)
    store.set_status(a.id, TicketStatus.RESOLVED, now=NOW)
    assert store.find_open_by_fingerprint("redis:oom") is None
    assert store.get(a.id).status == TicketStatus.RESOLVED


def test_escalated_ticket_stays_open(store):
    a = make(store)
    store.set_status(a.id, TicketStatus.ESCALATED, now=NOW)
    # escalated = assigned to a human but still an open, unresolved incident
    assert store.find_open_by_fingerprint("redis:oom").id == a.id


def test_comments_append_in_order(store):
    a = make(store)
    store.add_comment(a.id, author="agent", body="diagnosis posted", now=NOW)
    store.add_comment(a.id, author="agent", body="action proposed", now=NOW)
    comments = store.get(a.id).comments
    assert [c.body for c in comments] == ["diagnosis posted", "action proposed"]
    assert comments[0].author == "agent"


def test_assign_records_assignee(store):
    a = make(store)
    store.assign(a.id, "oncall@example.com", now=NOW)
    assert store.get(a.id).assignee == "oncall@example.com"


def test_persists_across_store_reopen(tmp_path):
    path = tmp_path / "tickets.db"
    first = SqliteTicketStore(path)
    created = make(first)
    first.add_comment(created.id, author="agent", body="note", now=NOW)
    # a fresh store on the same file — survives an agent restart
    second = SqliteTicketStore(path)
    got = second.get(created.id)
    assert got is not None and got.comments[0].body == "note"
    # sequence continues, doesn't restart at SRE-1
    assert make(second, fingerprint="x:y").id == "SRE-2"


def test_list_open_excludes_resolved(store):
    a = make(store)
    b = make(store, fingerprint="worker:silence")
    store.set_status(b.id, TicketStatus.RESOLVED, now=NOW)
    open_ids = {t.id for t in store.list_open()}
    assert open_ids == {a.id}
