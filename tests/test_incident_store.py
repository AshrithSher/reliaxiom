"""TDD: incident persistence. State survives an agent restart (mid-incident resume), and
the store answers the dedup/flap questions the manager asks."""
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def make(idx="INC-1", fingerprint="redis:unreachable", state=IncidentState.DETECTED):
    return Incident(
        id=idx, fingerprint=fingerprint, ticket_id="SRE-1", state=state,
        root_service="redis", services=["api", "worker"], fault_type="unreachable",
        severity="High", first_seen=NOW, updated_at=NOW,
    )


@pytest.fixture
def store(tmp_path):
    return IncidentStore(tmp_path / "incidents.db")


def test_save_and_get_roundtrip(store):
    store.save(make())
    got = store.get("INC-1")
    assert got is not None
    assert got.services == ["api", "worker"] and got.state is IncidentState.DETECTED


def test_save_is_upsert(store):
    inc = make()
    store.save(inc)
    inc.state = IncidentState.DIAGNOSING
    store.save(inc)
    assert store.get("INC-1").state is IncidentState.DIAGNOSING


def test_find_open_by_fingerprint_ignores_resolved(store):
    store.save(make(state=IncidentState.RESOLVED))
    assert store.find_open_by_fingerprint("redis:unreachable") is None
    store.save(make(idx="INC-2", state=IncidentState.DIAGNOSING))
    assert store.find_open_by_fingerprint("redis:unreachable").id == "INC-2"


def test_escalated_counts_as_open(store):
    store.save(make(state=IncidentState.ESCALATED))
    assert store.find_open_by_fingerprint("redis:unreachable").id == "INC-1"


def test_find_latest_resolved(store):
    older = make(idx="INC-1", state=IncidentState.RESOLVED)
    older.resolved_at = NOW
    newer = make(idx="INC-2", state=IncidentState.RESOLVED)
    newer.resolved_at = NOW + timedelta(minutes=5)
    store.save(older)
    store.save(newer)
    assert store.find_latest_resolved("redis:unreachable").id == "INC-2"


def test_persists_across_restart(tmp_path):
    path = tmp_path / "incidents.db"
    IncidentStore(path).save(make(state=IncidentState.ACTING))
    # fresh store on same file = agent restarted mid-incident
    resumed = IncidentStore(path).get("INC-1")
    assert resumed is not None and resumed.state is IncidentState.ACTING
