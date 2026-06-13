"""TDD: the change log. Records deployments/config changes and (from M4) the agent's own
actions, each timestamped. Diagnosis reads recent entries before symptom onset; M4 detection
will read it to ignore anomalies caused by the agent's own remediation (invariant #5)."""
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.changelog import ChangeLog, ChangeLogEntry

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def log(tmp_path):
    return ChangeLog(tmp_path / "changes.db")


def entry(off_s, actor="deployer", service="api", change_type="deploy", detail="v2"):
    return ChangeLogEntry(ts=NOW + timedelta(seconds=off_s), actor=actor, service=service,
                          change_type=change_type, detail=detail)


def test_record_and_recent(log):
    log.record(entry(0))
    got = log.recent(since=NOW - timedelta(seconds=10), until=NOW + timedelta(seconds=10))
    assert len(got) == 1 and got[0].service == "api"


def test_recent_respects_window(log):
    log.record(entry(-300))   # old
    log.record(entry(-5))     # recent
    got = log.recent(since=NOW - timedelta(seconds=60), until=NOW)
    assert [e.detail for e in got] == ["v2"] and len(got) == 1


def test_recent_ordered_by_time(log):
    log.record(entry(-5, detail="b"))
    log.record(entry(-20, detail="a"))
    got = log.recent(since=NOW - timedelta(seconds=60), until=NOW)
    assert [e.detail for e in got] == ["a", "b"]


def test_persists_across_restart(tmp_path):
    path = tmp_path / "changes.db"
    ChangeLog(path).record(entry(0, actor="sre-agent", change_type="restart_container"))
    got = ChangeLog(path).recent(since=NOW - timedelta(seconds=10), until=NOW + timedelta(seconds=10))
    assert got[0].actor == "sre-agent"
