"""TDD: the Postgres-backed stores (Maturity 9 / D-041) must behave IDENTICALLY to the SQLite
stores behind the same interfaces — that identity is the whole point of the swap. Each store's
behavior is asserted once, parametrized over both backends. The Postgres parameter is skipped
unless $SRE_STATE_DSN points at a reachable Postgres (so the offline suite stays green with no
Postgres); CI / the live verification run sets it and exercises the real shared store."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.changelog import ChangeLog, ChangeLogEntry
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore
from sre_agent.integrations.postmortems import PostMortem, SqlitePostMortemStore
from sre_agent.integrations.ticketing import SqliteTicketStore, TicketStatus

T0 = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
_DSN = os.environ.get("SRE_STATE_DSN", "")
_pg = pytest.mark.skipif(not _DSN, reason="set SRE_STATE_DSN to exercise the Postgres backend")

BACKENDS = ["sqlite", pytest.param("postgres", marks=_pg)]


def _reset_pg() -> None:
    from sre_agent.pgdb import connect_pg
    conn = connect_pg(_DSN)
    conn.execute("DROP TABLE IF EXISTS incidents, tickets, comments, postmortems, changes CASCADE")
    conn.execute("DROP SEQUENCE IF EXISTS ticket_seq")
    conn.close()


@pytest.fixture
def backend(request, tmp_path):
    if request.param == "postgres":
        _reset_pg()
    return request.param, tmp_path


def _incident_store(backend):
    kind, tmp = backend
    if kind == "postgres":
        from sre_agent.incident.pg_store import PostgresIncidentStore
        return PostgresIncidentStore(_DSN)
    return IncidentStore(tmp / "i.db")


def _ticket_store(backend):
    kind, tmp = backend
    if kind == "postgres":
        from sre_agent.integrations.pg_ticketing import PostgresTicketStore
        return PostgresTicketStore(_DSN)
    return SqliteTicketStore(tmp / "t.db")


def _pm_store(backend):
    kind, tmp = backend
    if kind == "postgres":
        from sre_agent.integrations.pg_postmortems import PostgresPostMortemStore
        return PostgresPostMortemStore(_DSN, markdown_dir=tmp / "pm")
    return SqlitePostMortemStore(tmp / "pm.db", markdown_dir=tmp / "pm")


def _changelog(backend):
    kind, tmp = backend
    if kind == "postgres":
        from sre_agent.pg_changelog import PostgresChangeLog
        return PostgresChangeLog(_DSN)
    return ChangeLog(tmp / "c.db")


def _incident(iid="INC-1", fp="redis:unreachable", state=IncidentState.DETECTED,
              at=T0) -> Incident:
    return Incident(id=iid, fingerprint=fp, ticket_id="SRE-1", state=state,
                    root_service="redis", services=["api", "worker"], fault_type="unreachable",
                    severity="High", first_seen=at, updated_at=at)


# --- incident store --------------------------------------------------------------
@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
def test_incident_save_get_roundtrip(backend):
    store = _incident_store(backend)
    inc = _incident()
    inc.root_cause = "redis OOM"
    inc.pending_params = {"service": "redis"}
    store.save(inc)
    got = store.get("INC-1")
    assert got is not None
    assert got.fingerprint == "redis:unreachable"
    assert got.services == ["api", "worker"]
    assert got.root_cause == "redis OOM"
    assert got.pending_params == {"service": "redis"}


@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
def test_incident_upsert_and_queries(backend):
    store = _incident_store(backend)
    store.save(_incident())
    assert store.find_open_by_fingerprint("redis:unreachable") is not None
    assert len(store.find_active()) == 1
    # resolve it → no longer open/active, but is the latest resolved
    inc = store.get("INC-1")
    inc.transition(IncidentState.DIAGNOSING, T0 + timedelta(seconds=1))
    inc.transition(IncidentState.VERIFYING, T0 + timedelta(seconds=2))
    inc.transition(IncidentState.RESOLVED, T0 + timedelta(seconds=3))
    store.save(inc)
    assert store.find_open_by_fingerprint("redis:unreachable") is None
    assert store.find_active() == []
    assert store.find_latest_resolved("redis:unreachable").id == "INC-1"


# --- ticket store ----------------------------------------------------------------
@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
def test_ticket_lifecycle_and_sequential_ids(backend):
    store = _ticket_store(backend)
    t1 = store.create(fingerprint="redis:unreachable", title="t1", services=["api"],
                      fault_type="unreachable", severity="High", evidence="e", now=T0)
    t2 = store.create(fingerprint="auth:unreachable", title="t2", services=["auth"],
                      fault_type="unreachable", severity="High", evidence="e", now=T0)
    assert t1.id == "SRE-1" and t2.id == "SRE-2"   # atomic sequence
    store.add_comment(t1.id, author="agent", body="diagnosing", now=T0)
    store.set_status(t1.id, TicketStatus.VERIFYING, now=T0)
    store.assign(t1.id, "oncall", now=T0)
    got = store.get(t1.id)
    assert got.status is TicketStatus.VERIFYING
    assert got.assignee == "oncall"
    assert [c.body for c in got.comments] == ["diagnosing"]
    assert {t.id for t in store.list_open()} == {"SRE-1", "SRE-2"}
    assert store.find_open_by_fingerprint("redis:unreachable").id == "SRE-1"
    store.set_status(t1.id, TicketStatus.RESOLVED, now=T0)
    assert {t.id for t in store.list_open()} == {"SRE-2"}


# --- change log ------------------------------------------------------------------
@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
def test_changelog_record_and_window(backend):
    log = _changelog(backend)
    log.record(ChangeLogEntry(ts=T0, actor="deployer", service="api", change_type="deploy",
                              detail="v2"))
    log.record(ChangeLogEntry(ts=T0 + timedelta(seconds=30), actor="sre-agent", service="redis",
                              change_type="restart_container", detail="auto"))
    rows = log.recent(T0 - timedelta(seconds=1), T0 + timedelta(seconds=60))
    assert [r.service for r in rows] == ["api", "redis"]
    assert log.recent(T0 + timedelta(seconds=100), T0 + timedelta(seconds=200)) == []


# --- post-mortems ----------------------------------------------------------------
@pytest.mark.parametrize("backend", BACKENDS, indirect=True)
def test_postmortem_record_and_fingerprint_query(backend):
    store = _pm_store(backend)
    store.record(PostMortem(incident_id="INC-1", ticket_id="SRE-1", fingerprint="redis:unreachable",
                            root_service="redis", services=["api"], root_cause="OOM",
                            action_taken="restart", outcome="resolved", time_to_resolve_s=42.0,
                            created_at=T0, timeline=["t0 detected", "t1 resolved"]))
    recent = store.recent_for_fingerprint("redis:unreachable")
    assert len(recent) == 1
    assert recent[0].root_cause == "OOM"
    assert recent[0].time_to_resolve_s == 42.0
    assert recent[0].timeline == ["t0 detected", "t1 resolved"]
    assert len(store.all()) == 1
    assert store.recent_for_fingerprint("nope:nope") == []
