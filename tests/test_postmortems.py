"""TDD: post-mortem store (M6 write path). Structured records are queryable by fingerprint
(the read path / incident memory) and also rendered to a markdown file (the human artifact,
and the Confluence-swap target at M7)."""
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.integrations.postmortems import (
    PostMortem,
    SqlitePostMortemStore,
    health_report,
    render_markdown,
)

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def pm(incident_id="INC-1", fingerprint="redis:unreachable", outcome="resolved", at=NOW):
    return PostMortem(
        incident_id=incident_id, ticket_id="SRE-1", fingerprint=fingerprint,
        root_service="redis", services=["api", "worker"], root_cause="redis maxmemory exhausted",
        action_taken="restart_container {'service': 'redis'}", outcome=outcome,
        time_to_resolve_s=142.0, created_at=at,
        timeline=["12:00:00 detected", "12:01:00 diagnosed", "12:02:22 resolved"])


@pytest.fixture
def store(tmp_path):
    return SqlitePostMortemStore(tmp_path / "pm.db", markdown_dir=tmp_path / "postmortems")


def test_record_and_query_by_fingerprint(store):
    store.record(pm())
    found = store.recent_for_fingerprint("redis:unreachable")
    assert len(found) == 1
    assert found[0].root_cause == "redis maxmemory exhausted"
    assert found[0].services == ["api", "worker"]


def test_query_filters_by_fingerprint(store):
    store.record(pm(incident_id="INC-1", fingerprint="redis:unreachable"))
    store.record(pm(incident_id="INC-2", fingerprint="worker:silence"))
    assert len(store.recent_for_fingerprint("redis:unreachable")) == 1
    assert store.recent_for_fingerprint("nothing:here") == []


def test_query_is_recent_first_and_limited(store):
    for i in range(5):
        store.record(pm(incident_id=f"INC-{i}", at=NOW + timedelta(minutes=i)))
    found = store.recent_for_fingerprint("redis:unreachable", limit=2)
    assert [p.incident_id for p in found] == ["INC-4", "INC-3"]   # newest first


def test_writes_markdown_file(tmp_path):
    store = SqlitePostMortemStore(tmp_path / "pm.db", markdown_dir=tmp_path / "pms")
    store.record(pm())
    md = (tmp_path / "pms" / "INC-1.md").read_text(encoding="utf-8")
    assert "redis maxmemory exhausted" in md
    assert "redis:unreachable" in md


def test_render_markdown_has_key_fields():
    text = render_markdown(pm())
    assert "# Post-mortem" in text and "redis:unreachable" in text
    assert "redis maxmemory exhausted" in text
    assert "restart_container" in text
    assert "Time to resolve" in text


def test_health_report_aggregates():
    pms = [
        pm(incident_id="INC-1", fingerprint="redis:unreachable", outcome="resolved"),
        pm(incident_id="INC-2", fingerprint="redis:unreachable", outcome="resolved"),
        pm(incident_id="INC-3", fingerprint="worker:silence", outcome="escalated"),
    ]
    report = health_report(pms)
    assert "Incidents: **3**" in report
    assert "auto-resolved: **2/3**" in report
    assert "redis:unreachable: 2×" in report      # recurring fault surfaced


def test_health_report_empty():
    assert "No incidents" in health_report([])


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "pm.db"
    SqlitePostMortemStore(path).record(pm())
    found = SqlitePostMortemStore(path).recent_for_fingerprint("redis:unreachable")
    assert found and found[0].incident_id == "INC-1"
