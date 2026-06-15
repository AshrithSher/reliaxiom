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


def cand(service, signal, codes=None, at_s=0.0, traces=None):
    return IncidentCandidate(
        services=[service], signal_type=signal, detail=f"{service} {signal}",
        first_seen=T0, confirmed_at=T0 + timedelta(seconds=at_s),
        error_codes=codes or [], trace_ids=traces or [],
    )


class _Change:
    """A minimal change event (duck-typed ChangeLogEntry) for correlation tests."""
    def __init__(self, service, at_s=-30.0, actor="deployer"):
        self.service = service
        self.ts = T0 + timedelta(seconds=at_s)
        self.actor = actor


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


# --- P1.1 multi-signal fusion (D-040) ---------------------------------------------

def test_memleak_same_service_heterogeneous_signals_fuse_to_one_incident():
    # a memory leak trips crash_loop AND error_rate on the SAME service, with no shared
    # error code. The old priority cascade left these as two tickets; same-service fusion
    # collapses them into one (the D-014 break).
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("api", "crash_loop"),
        cand("api", "error_rate"),
    ])
    assert len(groups) == 1
    assert groups[0].root_service == "api"
    assert set(groups[0].services) == {"api"}
    assert {c.signal_type for c in groups[0].candidates} == {"crash_loop", "error_rate"}


def test_latency_fault_latency_and_error_signals_fuse():
    # added latency trips latency_p95 and (via timeouts) error_rate on loadgen — one fault.
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("loadgen", "latency_p95"),
        cand("loadgen", "error_rate"),
    ])
    assert len(groups) == 1
    assert set(groups[0].services) == {"loadgen"}


def test_cross_modality_log_metric_trace_collapse_on_one_service():
    # the log error_rate twin, the Prometheus 5xx-ratio twin, and the Tempo error-span twin
    # of one fault all land on api and must be ONE incident, not three.
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("api", "error_rate"),
        cand("api", "metric_error_ratio"),
        cand("api", "trace_error_rate"),
    ])
    assert len(groups) == 1
    assert groups[0].root_service == "api"


def test_shared_trace_id_fuses_services_topology_alone_would_not():
    # api and worker are not on each other's dependency chain (siblings over redis/postgres),
    # so topology gives no edge and there is no error code. A shared request/trace id is the
    # causal signal that they are the SAME failing requests → one incident.
    groups = Correlator(LAB_TOPOLOGY).correlate([
        cand("api", "error_rate", traces=["req-abc"]),
        cand("worker", "error_rate", traces=["req-abc"]),
    ])
    assert len(groups) == 1
    assert set(groups[0].services) == {"api", "worker"}


def test_shared_trace_fuses_even_beyond_the_time_window():
    # a shared trace id is causal regardless of timing — a late symptom on the same trace
    # still belongs to the same incident even if it confirms minutes later.
    groups = Correlator(LAB_TOPOLOGY, max_span_s=60.0).correlate([
        cand("api", "error_rate", traces=["req-xyz"], at_s=0.0),
        cand("worker", "error_rate", traces=["req-xyz"], at_s=600.0),
    ])
    assert len(groups) == 1


def test_same_service_signals_beyond_window_do_not_fuse():
    # without a shared trace, two same-service candidates far apart in time are distinct events
    groups = Correlator(LAB_TOPOLOGY, max_span_s=60.0).correlate([
        cand("api", "crash_loop", at_s=0.0),
        cand("api", "error_rate", at_s=600.0),
    ])
    assert len(groups) == 2


def test_deploy_coincidence_picks_the_changed_service_as_root():
    # api and worker fuse on a shared trace; neither is upstream of the other, so topology
    # can't pick a root. A recent deploy to worker is the causal pointer → root = worker.
    groups = Correlator(LAB_TOPOLOGY).correlate(
        [
            cand("api", "error_rate", traces=["t1"]),
            cand("worker", "error_rate", traces=["t1"]),
        ],
        change_events=[_Change("worker", at_s=-20.0)],
        now=T0 + timedelta(seconds=30),
    )
    assert len(groups) == 1
    assert groups[0].root_service == "worker"


def test_dependency_error_code_outranks_a_coincident_deploy():
    # an actual redis_unreachable error code is more precise than a deploy coincidence:
    # the root stays redis even if something was deployed to api.
    groups = Correlator(LAB_TOPOLOGY).correlate(
        [
            cand("api", "error_rate", codes=["redis_unreachable"]),
            cand("worker", "silence"),
        ],
        change_events=[_Change("api", at_s=-20.0)],
        now=T0 + timedelta(seconds=30),
    )
    assert fingerprints(groups) == ["redis:unreachable"]


def test_a_deploy_does_not_merge_unrelated_candidates():
    # change events influence root choice, never grouping: a deploy to gateway must not fuse
    # an unrelated worker silence with a gateway error spike.
    groups = Correlator(LAB_TOPOLOGY).correlate(
        [cand("worker", "silence"), cand("gateway", "error_rate")],
        change_events=[_Change("gateway", at_s=-20.0)],
        now=T0 + timedelta(seconds=30),
    )
    assert len(groups) == 2
