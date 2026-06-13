"""TDD: the context assembler. Builds the (system, user) prompt from the incident + the log
window, in the D-012 priority order, under a character budget. The system prompt must carry
the action catalog and the strict-JSON contract (no tier/confidence)."""
from datetime import datetime, timedelta, timezone

from sre_agent.config import Config
from sre_agent.diagnosis.context import ContextAssembler
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import LogRecord

T0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def rec(service, off, **kw):
    return LogRecord(ts=T0 + timedelta(seconds=off), service=service, raw={"service": service, **kw}, **kw)


def incident():
    return Incident(
        id="INC-1", fingerprint="redis:unreachable", ticket_id="SRE-1",
        state=IncidentState.DETECTED, root_service="redis", services=["api", "worker"],
        fault_type="unreachable", severity="High", first_seen=T0, updated_at=T0,
    )


def seeded_window():
    w = SlidingWindow()
    # a cross-service request trace sharing a request_id, with an error
    w.append(rec("api", 580, level="ERROR", event="request_failed", status=503,
                 request_id="rid-1", error="redis_unreachable"))
    w.append(rec("worker", 581, level="ERROR", event="job_failed", request_id="rid-1",
                 error="redis_unreachable"))
    # a neighbor service log
    w.append(rec("gateway", 585, level="INFO", event="request", status=200, request_id="rid-2"))
    return w


def assembler():
    return ContextAssembler(LAB_TOPOLOGY, Config())


def test_system_prompt_has_catalog_and_json_contract():
    system, _ = assembler().build(incident(), seeded_window(), now=T0 + timedelta(seconds=600))
    assert "restart_container" in system          # catalog present
    assert "JSON" in system or "json" in system
    assert "tier" in system.lower()               # explicitly tells the model not to set it


def test_user_prompt_has_incident_and_affected_services():
    _, user = assembler().build(incident(), seeded_window(), now=T0 + timedelta(seconds=600))
    assert "redis:unreachable" in user
    assert "api" in user and "worker" in user


def test_user_prompt_includes_correlated_trace():
    _, user = assembler().build(incident(), seeded_window(), now=T0 + timedelta(seconds=600))
    assert "rid-1" in user                         # the cross-service request id


def test_budget_is_respected():
    cfg = Config()
    cfg.diagnosis_budget_chars = 400
    _, user = ContextAssembler(LAB_TOPOLOGY, cfg).build(
        incident(), seeded_window(), now=T0 + timedelta(seconds=600))
    assert len(user) <= 400


def test_includes_live_signals_when_provided():
    from sre_agent.models import ContainerState, SignalSnapshot
    snap = SignalSnapshot(
        ts=T0, containers={"worker": ContainerState(name="worker", status="exited", restart_count=3)},
        health={"api": True}, redis_queue_depth=512, pg_connections=40)
    _, user = assembler().build(incident(), seeded_window(), now=T0 + timedelta(seconds=600),
                                signals=snap)
    assert "queue_depth=512" in user or "512" in user
    assert "exited" in user                          # the read-only docker signal


def test_includes_recent_changes_when_changelog_provided(tmp_path):
    from sre_agent.changelog import ChangeLog, ChangeLogEntry
    cl = ChangeLog(tmp_path / "c.db")
    cl.record(ChangeLogEntry(ts=T0 - timedelta(seconds=30), actor="deployer",
                             service="api", change_type="deploy", detail="api v2.3 shipped"))
    a = ContextAssembler(LAB_TOPOLOGY, Config(), changelog=cl)
    _, user = a.build(incident(), seeded_window(), now=T0 + timedelta(seconds=600))
    assert "v2.3" in user and "deploy" in user        # change-log lookup surfaced


def test_includes_incident_memory_for_known_fingerprint(tmp_path):
    from datetime import datetime, timezone
    from sre_agent.integrations.postmortems import PostMortem, SqlitePostMortemStore
    pms = SqlitePostMortemStore(tmp_path / "pm.db")
    pms.record(PostMortem(
        incident_id="INC-0", ticket_id="SRE-0", fingerprint="redis:unreachable",
        root_service="redis", services=["api", "worker"],
        root_cause="redis maxmemory exhausted", action_taken="restart_container redis",
        outcome="resolved", time_to_resolve_s=120.0,
        created_at=datetime(2026, 6, 11, tzinfo=timezone.utc)))
    a = ContextAssembler(LAB_TOPOLOGY, Config(), postmortems=pms)
    _, user = a.build(incident(), seeded_window(), now=T0 + timedelta(seconds=600))
    assert "INCIDENT MEMORY" in user
    assert "maxmemory exhausted" in user and "restart_container redis" in user


def test_no_memory_section_for_novel_fingerprint(tmp_path):
    from sre_agent.integrations.postmortems import SqlitePostMortemStore
    a = ContextAssembler(LAB_TOPOLOGY, Config(), postmortems=SqlitePostMortemStore(tmp_path / "pm.db"))
    _, user = a.build(incident(), seeded_window(), now=T0 + timedelta(seconds=600))
    assert "INCIDENT MEMORY" not in user   # first time we've seen this fault


def test_evidence_corpus_contains_real_log_tokens():
    a = assembler()
    corpus = a.evidence_corpus(incident(), seeded_window(), now=T0 + timedelta(seconds=600))
    # the verifier built from this corpus should accept a real request id and reject a fake one
    verify = a.make_verifier(corpus)
    assert verify("rid-1")
    assert not verify("rid-9999-fabricated")
