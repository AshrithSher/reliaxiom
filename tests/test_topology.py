"""TDD: the lab dependency graph and the queries correlation needs."""
from sre_agent.incident.topology import LAB_TOPOLOGY, TopologyMap


def test_transitive_dependencies():
    t = LAB_TOPOLOGY
    assert t.dependencies_of("api") == {"postgres", "redis", "auth"}
    assert t.dependencies_of("gateway") == {"webapp", "api", "postgres", "redis", "auth"}
    assert t.dependencies_of("worker") == {"redis", "postgres", "payments"}
    assert t.dependencies_of("redis") == set()


def test_transitive_dependents():
    t = LAB_TOPOLOGY
    assert t.dependents_of("redis") == {"api", "worker", "webapp", "gateway", "loadgen"}
    assert t.dependents_of("api") == {"webapp", "gateway", "loadgen"}
    assert t.dependents_of("auth") == {"api", "webapp", "gateway", "loadgen"}
    assert t.dependents_of("payments") == {"worker"}


def test_is_upstream_of():
    t = LAB_TOPOLOGY
    assert t.is_upstream_of("api", "gateway")     # gateway depends on api
    assert t.is_upstream_of("redis", "worker")
    assert not t.is_upstream_of("api", "worker")  # worker doesn't depend on api
    assert not t.is_upstream_of("api", "api")


def test_most_upstream_picks_common_ancestor():
    t = LAB_TOPOLOGY
    assert t.most_upstream({"api", "webapp", "gateway"}) == "api"
    assert t.most_upstream({"redis", "api", "worker"}) == "redis"


def test_most_upstream_none_when_unrelated():
    t = LAB_TOPOLOGY
    assert t.most_upstream({"api", "worker"}) is None   # neither depends on the other
    assert t.most_upstream({"worker", "gateway"}) is None


def test_common_dependencies():
    t = LAB_TOPOLOGY
    assert t.common_dependencies({"api", "worker"}) == {"redis", "postgres"}


def test_custom_topology():
    t = TopologyMap({"a": ["b"], "b": ["c"], "c": []})
    assert t.dependencies_of("a") == {"b", "c"}
    assert t.is_upstream_of("c", "a")
    assert t.most_upstream({"a", "b", "c"}) == "c"


def test_services_lists_every_node_sorted():
    # a dependency that is never itself a key (a leaf) is still a node
    t = TopologyMap({"a": ["b", "c"], "b": []})
    assert t.services() == ["a", "b", "c"]


def test_edges_are_public_service_dependency_pairs():
    # the public projection the dashboard needs, replacing reach-ins to _direct
    t = TopologyMap({"a": ["b", "c"], "b": ["c"], "c": []})
    assert set(t.edges()) == {("a", "b"), ("a", "c"), ("b", "c")}
    # a leaf with no deps contributes no edges but is still a service
    assert ("c", "a") not in t.edges()
