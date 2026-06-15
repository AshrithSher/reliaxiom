"""TDD: self-monitoring metrics (Maturity 11 / D-042) — the exposition format, the semantic
recorders, the /metrics endpoint, and the manager wiring (lifecycle → metrics, stream_blind →
agent-health page, never a ticket)."""
from __future__ import annotations

import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.config import Config
from sre_agent.health import HealthServer
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.notifications import InMemoryNotifier
from sre_agent.integrations.ticketing import SqliteTicketStore
from sre_agent.metrics import AgentMetrics, Counter, Gauge, Histogram
from sre_agent.models import IncidentCandidate

T0 = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)


# --- exposition primitives -------------------------------------------------------
def test_counter_renders_with_labels():
    c = Counter("sre_x_total", "x", ["kind"])
    c.inc(kind="a")
    c.inc(2, kind="a")
    c.inc(kind="b")
    text = "\n".join(c.render())
    assert "# TYPE sre_x_total counter" in text
    assert 'sre_x_total{kind="a"} 3' in text
    assert 'sre_x_total{kind="b"} 1' in text


def test_gauge_overwrites():
    g = Gauge("sre_g", "g")
    g.set(5)
    g.set(9)
    assert "sre_g 9" in "\n".join(g.render())


def test_histogram_buckets_are_cumulative():
    h = Histogram("sre_h_seconds", "h", buckets=(1, 10, 100))
    for v in (0.5, 5, 50, 500):
        h.observe(v)
    text = "\n".join(h.render())
    assert 'sre_h_seconds_bucket{le="1"} 1' in text
    assert 'sre_h_seconds_bucket{le="10"} 2' in text
    assert 'sre_h_seconds_bucket{le="100"} 3' in text
    assert 'sre_h_seconds_bucket{le="+Inf"} 4' in text
    assert "sre_h_seconds_count 4" in text
    assert "sre_h_seconds_sum 555.5" in text


def test_agent_metrics_render_is_well_formed():
    m = AgentMetrics()
    m.on_candidate("error_rate")
    m.on_incident_created("High", 12.0)
    m.on_resolved(120.0)
    m.on_escalated()
    m.on_action("restart_container", True)
    m.on_diagnosis(1.5, ok=True)
    m.heartbeat(leader=True)
    text = m.render()
    assert 'sre_candidates_total{signal_type="error_rate"} 1' in text
    assert 'sre_incidents_total{severity="High"} 1' in text
    assert 'sre_incident_outcomes_total{outcome="resolved"} 1' in text
    assert "sre_escalations_total 1" in text
    assert 'sre_actions_total{action="restart_container",result="success"} 1' in text
    assert "sre_leader 1" in text
    assert "sre_up 1" in text
    assert "sre_detection_latency_seconds_count 1" in text
    assert "sre_mttr_seconds_count 1" in text


def test_metrics_served_on_health_endpoint():
    import socket
    m = AgentMetrics()
    m.on_candidate("silence")
    s = socket.socket(); s.bind(("", 0)); port = s.getsockname()[1]; s.close()
    srv = HealthServer(port)
    srv.register("/metrics", lambda: (200, "text/plain; version=0.0.4", m.render()))
    srv.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as r:
            body = r.read().decode()
        assert r.status == 200
        assert 'sre_candidates_total{signal_type="silence"} 1' in body
    finally:
        srv.stop()


# --- manager lifecycle wiring ----------------------------------------------------
@pytest.fixture
def mgr(tmp_path):
    cfg = Config()
    cfg.correlation_window_s = 5.0
    metrics = AgentMetrics()
    notifier = InMemoryNotifier()
    m = IncidentManager(IncidentStore(tmp_path / "i.db"), SqliteTicketStore(tmp_path / "t.db"),
                        notifier, LAB_TOPOLOGY, cfg, metrics=metrics)
    return m, metrics, notifier


def _cand(service, signal, at_s=0.0):
    return IncidentCandidate(services=[service], signal_type=signal, detail=f"{service} {signal}",
                             first_seen=T0, confirmed_at=T0 + timedelta(seconds=at_s))


def test_incident_creation_and_resolution_recorded(mgr):
    m, metrics, _ = mgr
    m.ingest(_cand("worker", "silence"), now=T0)
    inc = m.tick(now=T0 + timedelta(seconds=10))[0]
    text = metrics.render()
    assert 'sre_incidents_total{severity="Medium"} 1' in text
    assert "sre_detection_latency_seconds_count 1" in text
    m.resolve(inc.id, now=T0 + timedelta(seconds=70))
    assert 'sre_incident_outcomes_total{outcome="resolved"} 1' in metrics.render()
    assert "sre_mttr_seconds_count 1" in metrics.render()


def test_stream_blind_is_agent_health_page_not_a_ticket(mgr):
    m, metrics, notifier = mgr
    m.ingest(_cand("_stream", "stream_blind"), now=T0)
    created = m.tick(now=T0 + timedelta(seconds=10))
    assert created == []                                  # NOT correlated/ticketed
    assert m._tickets.list_open() == []                   # no lab ticket
    assert "sre_stream_blind_total 1" in metrics.render()
    assert [n.kind for n in notifier.sent] == ["agent_health"]
