"""TDD: correlation — collapse a multi-symptom fault into ONE incident attributed to the
most upstream suspect. This is the heart of 'one fault = one ticket'.

Attribution priority:
  1. dependency error codes (redis_unreachable → redis) — precise, evidence-driven
  2. topology: among remaining candidates, the one all others depend on is the root
  3. leftovers: each candidate is its own incident
Candidates that merely share a common dependency are NOT merged on that alone — that would
fuse every unrelated worker+gateway blip (they both depend on redis/postgres).
"""
from datetime import datetime, timedelta, timezone

from sre_agent.incident.correlation import Correlator
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.models import IncidentCandidate

T0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def cand(service, signal, codes=None, at_s=0.0):
    return IncidentCandidate(
        services=[service], signal_type=signal, detail=f"{service} {signal}",
        first_seen=T0, confirmed_at=T0 + timedelta(seconds=at_s),
        error_codes=codes or [],
    )


def fingerprints(groups):
    return sorted(g.fingerprint for g in groups)


def test_redis_oom_cascade_is_one_incident_rooted_at_redis():
    # api errors carry redis_unreachable; worker goes silent (no codes) but depends on redis
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("api", "error_rate", codes=["redis_unreachable"]),
        cand("worker", "silence"),
    ])
    assert len(groups) == 1
    g = groups[0]
    assert g.root_service == "redis"
    assert g.fingerprint == "redis:unreachable"
    assert set(g.services) == {"api", "worker"}


def test_kill_db_cascade_is_one_incident_rooted_at_postgres():
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("api", "error_rate", codes=["db_unreachable"]),
        cand("webapp", "error_rate", codes=["db_unreachable"]),
        cand("worker", "error_rate", codes=["db_unreachable"]),
    ])
    assert fingerprints(groups) == ["postgres:unreachable"]
    assert set(groups[0].services) == {"api", "webapp", "worker"}


def test_api_error_spike_cascade_rooted_at_api_via_topology():
    # api 500s (internal_error, not a dependency code) ripple to its dependents
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("api", "error_rate", codes=["internal_error"]),
        cand("webapp", "error_rate"),
        cand("gateway", "error_rate"),
    ])
    assert len(groups) == 1
    assert groups[0].root_service == "api"
    assert set(groups[0].services) == {"api", "webapp", "gateway"}


def test_auth_outage_cascade_is_one_incident_rooted_at_auth():
    # auth down: api can't authenticate, so api + every consumer error with auth_unreachable.
    # The whole fan-out must collapse to a single auth:unreachable incident.
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("api", "error_rate", codes=["auth_unreachable"]),
        cand("webapp", "error_rate", codes=["auth_unreachable"]),
        cand("gateway", "error_rate"),
        cand("loadgen", "error_rate"),
    ])
    assert fingerprints(groups) == ["auth:unreachable"]
    assert set(groups[0].services) == {"api", "webapp", "gateway", "loadgen"}


def test_payments_outage_cascade_is_one_incident_rooted_at_payments():
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("worker", "error_rate", codes=["payments_unreachable"]),
    ])
    assert fingerprints(groups) == ["payments:unreachable"]
    assert groups[0].root_service == "payments"


def test_isolated_silence_is_its_own_incident():
    groups = Correlator(LAB_TOPOLOGY).correlate([cand("worker", "silence")])
    assert fingerprints(groups) == ["worker:silence"]


def test_unrelated_faults_stay_separate():
    # worker silence and a gateway error spike share no upstream chain → two incidents
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("worker", "silence"),
        cand("gateway", "error_rate"),
    ])
    assert len(groups) == 2
    assert "worker:silence" in fingerprints(groups)


def test_empty_input():
    assert Correlator(LAB_TOPOLOGY).correlate([]) == []


def test_stream_blind_is_never_merged_into_a_lab_incident():
    # the agent-health signal must stand alone, not get folded into a service incident
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("_stream", "stream_blind"),
        cand("api", "error_rate", codes=["redis_unreachable"]),
    ])
    assert "_stream:stream_blind" in fingerprints(groups)
    assert any(g.root_service == "redis" for g in groups)
    assert len(groups) == 2
