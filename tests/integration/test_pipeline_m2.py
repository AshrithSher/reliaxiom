"""Integration tests for the M2 exit criteria — REAL detection engine + REAL Incident
Manager, driven by synthetic log streams that mimic the lab's actual fault signatures.

These close what unit tests could not: that the real detectors emit the error codes the
correlator keys on, and that a real cascade collapses to exactly one ticket.
"""
import pytest

from sre_agent.incident.lifecycle import IncidentState
from tests.integration.harness import PipelineHarness

ALL_SERVICES = ["gateway", "webapp", "api", "worker", "loadgen"]


def baseline(h: PipelineHarness, t: float) -> None:
    """Every service logging healthily — keeps silence detectors quiet."""
    for svc in ALL_SERVICES:
        h.emit(svc, t, level="INFO", event="request", status=200, request_id=f"ok{int(t)}")


def drive(h: PipelineHarness, emit, start: float, end: float, step: float = 2.0):
    acted = []
    t = start
    while t <= end:
        baseline(h, t)
        emit(h, t)
        acted += h.advance_to(t)
        t += step
    return acted


# --- kill-db: full cascade collapses to ONE postgres ticket ----------------------
def emit_db_down(h, t):
    # api/webapp/worker know the cause (db_unreachable); loadgen just sees 503s
    for svc in ("api", "webapp", "worker"):
        h.emit(svc, t, level="ERROR", event="request_failed", status=503,
               request_id=f"db{int(t)}", error="db_unreachable")
    h.emit("loadgen", t, level="ERROR", event="request", status=503, request_id=f"lg{int(t)}")


def test_kill_db_cascade_is_exactly_one_postgres_ticket(tmp_path):
    h = PipelineHarness(tmp_path)
    drive(h, emit_db_down, start=20, end=60)
    tickets = h.open_tickets()
    assert len(tickets) == 1, [t.fingerprint for t in tickets]
    assert tickets[0].fingerprint == "postgres:unreachable"
    assert "loadgen" in tickets[0].services       # user-facing → severity High
    assert tickets[0].severity == "High"


# --- kill-redis: graceful degradation, worker errors → ONE redis ticket -----------
def emit_redis_down(h, t):
    # worker logs redis_down at ERROR; api only WARNs (cache_degraded) and keeps serving
    h.emit("worker", t, level="ERROR", event="redis_down", request_id=f"w{int(t)}",
           error="redis_unreachable")
    h.emit("api", t, level="WARNING", event="cache_degraded", status=200,
           request_id=f"a{int(t)}", error="redis_unreachable")


def test_kill_redis_is_one_redis_ticket(tmp_path):
    h = PipelineHarness(tmp_path)
    drive(h, emit_redis_down, start=20, end=60)
    tickets = h.open_tickets()
    assert len(tickets) == 1, [t.fingerprint for t in tickets]
    assert tickets[0].fingerprint == "redis:unreachable"
    # api WARNs don't trip error-rate (it's still serving) — only worker is implicated
    assert tickets[0].services == ["worker"]


# --- dedup: a sustained fault makes one ticket, many comments --------------------
def test_sustained_fault_dedups_to_one_ticket(tmp_path):
    h = PipelineHarness(tmp_path)
    # run well past the 60s cooldown so the detector re-fires the same fingerprint
    drive(h, emit_db_down, start=20, end=130)
    tickets = h.open_tickets()
    assert len(tickets) == 1
    comments = h.tickets.get(tickets[0].id).comments
    assert any("recur" in c.body.lower() for c in comments)


# --- null: a healthy stream produces zero tickets --------------------------------
def test_healthy_stream_is_silent(tmp_path):
    h = PipelineHarness(tmp_path)
    drive(h, lambda _h, _t: None, start=0, end=120)
    assert h.open_tickets() == []


# --- flap: resolve then recur within the window reopens the same ticket ----------
def test_flap_reopens_same_ticket(tmp_path):
    h = PipelineHarness(tmp_path)
    created = drive(h, emit_db_down, start=20, end=55)
    assert len(created) == 1
    inc = created[0]
    h.manager.resolve(inc.id, now=_at(60))
    # fault recurs within the flap window
    drive(h, emit_db_down, start=70, end=110)
    reopened = h.incidents.get(inc.id)
    assert reopened.state is IncidentState.FLAPPING
    assert len(h.open_tickets()) == 1


def _at(offset_s):
    from tests.integration.harness import T0
    from datetime import timedelta
    return T0 + timedelta(seconds=offset_s)
