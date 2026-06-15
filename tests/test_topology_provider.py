"""TDD: the TopologyProvider SPI (P0.1 / D-031) — the dependency graph is read through an
interface so it can come from a real source (service-mesh / trace-graph / CMDB / Backstage)
without forking the engine. Same discipline as the telemetry adapters (D-032): the live
provider injects its transport, parses an adjacency document, and **degrades to the static
fallback on any failure** (D-015) — a momentarily-unreachable topology source must never
crash correlation or fabricate a graph."""
import json

from sre_agent.config import Config
from sre_agent.incident.topology import LAB_TOPOLOGY, TopologyMap
from sre_agent.incident.topology_provider import (
    HttpTopologyProvider,
    StaticTopologyProvider,
    TopologyProvider,
    build_topology_provider,
)


def _transport(status, body):
    """A canned transport(url) -> (status, text) for offline tests."""
    def get(url):
        return status, body
    return get


def test_static_provider_returns_its_map_and_conforms_to_protocol():
    p = StaticTopologyProvider(LAB_TOPOLOGY)
    assert isinstance(p, TopologyProvider)        # runtime_checkable Protocol
    assert p.topology() is LAB_TOPOLOGY
    assert p.topology().dependencies_of("api") == {"postgres", "redis", "auth"}


def test_http_provider_parses_adjacency_document():
    doc = {"a": ["b", "c"], "b": ["c"], "c": []}
    p = HttpTopologyProvider("http://cmdb", transport=_transport(200, json.dumps(doc)))
    assert isinstance(p, TopologyProvider)
    t = p.topology()
    assert isinstance(t, TopologyMap)
    assert t.dependencies_of("a") == {"b", "c"}
    assert t.is_upstream_of("c", "a")


def test_http_provider_caches_a_successful_fetch():
    calls = {"n": 0}

    def counting_transport(url):
        calls["n"] += 1
        return 200, json.dumps({"a": ["b"], "b": []})

    p = HttpTopologyProvider("http://cmdb", transport=counting_transport)
    p.topology()
    p.topology()
    assert calls["n"] == 1   # fetched once, then served from cache


def test_http_provider_degrades_to_fallback_on_http_error():
    p = HttpTopologyProvider("http://cmdb", transport=_transport(503, ""),
                             fallback=LAB_TOPOLOGY)
    assert p.topology() is LAB_TOPOLOGY


def test_http_provider_degrades_on_malformed_json():
    p = HttpTopologyProvider("http://cmdb", transport=_transport(200, "not json{"),
                             fallback=LAB_TOPOLOGY)
    assert p.topology() is LAB_TOPOLOGY


def test_http_provider_degrades_on_non_adjacency_shape():
    # a 200 with the wrong shape (not a dict of string->list[str]) must not be trusted
    for bad in (json.dumps([1, 2, 3]), json.dumps({"a": "b"}), json.dumps({"a": [1, 2]})):
        p = HttpTopologyProvider("http://cmdb", transport=_transport(200, bad),
                                 fallback=LAB_TOPOLOGY)
        assert p.topology() is LAB_TOPOLOGY


def test_http_provider_degrades_when_transport_raises():
    def boom(url):
        raise ConnectionError("network down")

    p = HttpTopologyProvider("http://cmdb", transport=boom, fallback=LAB_TOPOLOGY)
    assert p.topology() is LAB_TOPOLOGY  # never raises (D-015)


def test_http_provider_recovers_after_a_transient_failure():
    # a degrade must not be cached: once the source returns, the real graph is used
    state = {"ok": False}

    def flaky(url):
        if state["ok"]:
            return 200, json.dumps({"a": ["b"], "b": []})
        return 503, ""

    p = HttpTopologyProvider("http://cmdb", transport=flaky, fallback=LAB_TOPOLOGY)
    assert p.topology() is LAB_TOPOLOGY        # first call fails → fallback
    state["ok"] = True
    assert p.topology().dependencies_of("a") == {"b"}   # later call succeeds


def test_build_defaults_to_static_lab_topology():
    p = build_topology_provider(Config())
    assert isinstance(p, StaticTopologyProvider)
    assert p.topology() is LAB_TOPOLOGY


def test_build_selects_http_when_configured():
    cfg = Config()
    cfg.topology_source = "http"
    p = build_topology_provider(cfg)
    assert isinstance(p, HttpTopologyProvider)
