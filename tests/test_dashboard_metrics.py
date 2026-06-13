"""TDD: the dashboard ROI metrics aggregation. Pure function over the post-mortem archive
(closed incidents, with time-to-resolve) plus the live active incidents — no I/O, so the
business-value numbers (auto-resolve %, MTTR, pages avoided) are tested deterministically."""
from datetime import datetime, timedelta, timezone

from sre_agent.dashboard.metrics import compute_metrics
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.integrations.postmortems import PostMortem

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


def _pm(incident_id, fingerprint, outcome="resolved", ttr=90.0, services=None, created=NOW):
    return PostMortem(
        incident_id=incident_id, ticket_id="SRE-" + incident_id, fingerprint=fingerprint,
        root_service=fingerprint.split(":")[0], services=services or [fingerprint.split(":")[0]],
        root_cause="rc", action_taken="restart", outcome=outcome,
        time_to_resolve_s=ttr if outcome == "resolved" else None, created_at=created)


def _active(incident_id, fingerprint, state=IncidentState.DIAGNOSING):
    return Incident(
        id=incident_id, fingerprint=fingerprint, ticket_id=None, state=state,
        root_service=fingerprint.split(":")[0], services=[fingerprint.split(":")[0]],
        fault_type=fingerprint.split(":")[1], severity="High",
        first_seen=NOW, updated_at=NOW)


def test_empty_archive_is_all_zero_not_error():
    m = compute_metrics([], [])
    assert m["total"] == 0 and m["resolved"] == 0 and m["escalated"] == 0
    assert m["auto_resolve_rate"] == 0.0
    assert m["mttr_s"] is None
    assert m["pages_avoided"] == 0


def test_counts_and_auto_resolve_rate():
    pms = [_pm("1", "worker:silence"), _pm("2", "worker:silence"),
           _pm("3", "postgres:unreachable", outcome="escalated")]
    m = compute_metrics(pms, [])
    assert m["closed"] == 3
    assert m["resolved"] == 2 and m["escalated"] == 1
    assert m["auto_resolve_rate"] == 2 / 3
    assert m["pages_avoided"] == 2     # the 2 the agent closed with no human


def test_active_incidents_count_toward_total_not_resolved():
    pms = [_pm("1", "worker:silence")]
    active = [_active("9", "redis:oom")]
    m = compute_metrics(pms, active)
    assert m["closed"] == 1 and m["active"] == 1 and m["total"] == 2
    assert m["resolved"] == 1


def test_mttr_is_mean_over_resolved_only():
    pms = [_pm("1", "a:x", ttr=60.0), _pm("2", "a:x", ttr=120.0),
           _pm("3", "b:y", outcome="escalated")]  # escalated has no ttr → excluded
    m = compute_metrics(pms, [])
    assert m["mttr_s"] == 90.0
    assert m["fastest_resolve_s"] == 60.0


def test_breakdowns_by_fault_type_and_service():
    pms = [_pm("1", "worker:silence", services=["worker"]),
           _pm("2", "api:errors", services=["api", "webapp"]),
           _pm("3", "api:errors", services=["api", "webapp"])]
    m = compute_metrics(pms, [])
    assert m["by_fault_type"]["errors"] == 2
    assert m["by_fault_type"]["silence"] == 1
    assert m["by_service"]["api"] == 2 and m["by_service"]["webapp"] == 2
    assert m["by_service"]["worker"] == 1


def test_recurring_faults_only_lists_repeats_desc():
    pms = [_pm("1", "worker:silence"), _pm("2", "worker:silence"),
           _pm("3", "worker:silence"), _pm("4", "redis:oom")]
    m = compute_metrics(pms, [])
    assert m["recurring"] == [["worker:silence", 3]]   # redis:oom occurred once → excluded


def test_mttr_trend_is_resolved_chronological():
    earlier, later = NOW, NOW + timedelta(minutes=5)
    pms = [_pm("2", "a:x", ttr=120.0, created=later),
           _pm("1", "a:x", ttr=60.0, created=earlier),
           _pm("3", "b:y", outcome="escalated", created=later)]
    trend = compute_metrics(pms, [])["mttr_trend"]
    assert [t["incident_id"] for t in trend] == ["1", "2"]   # chrono, escalated omitted
    assert [t["ttr_s"] for t in trend] == [60.0, 120.0]
