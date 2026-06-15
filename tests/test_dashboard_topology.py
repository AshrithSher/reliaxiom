"""TDD: the dashboard topology/health projection must reflect the live container poll, not
just incidents. A stopped lab service has to surface red the instant the poll sees it down —
before detection has opened an incident — and clear the moment the operator restarts it. This
is what made a manual `docker stop`/`docker start` look like 'nothing's happening' in the UI."""
from sre_agent.dashboard.server import _topology


def _health(topo):
    return {n["id"]: n["health"] for n in topo["nodes"]}


def test_down_container_renders_red_without_an_incident():
    topo = _topology([], down={"postgres"})
    health = _health(topo)
    assert health["postgres"] == "root"     # red, the source of the problem
    assert health["api"] == "healthy"       # everything else stays green


def test_no_down_containers_is_all_healthy():
    health = _health(_topology([], down=set()))
    assert set(health.values()) == {"healthy"}


def test_down_set_is_intersected_with_topology_nodes():
    # an observability-stack container (not a lab service node) must not invent a node or
    # otherwise perturb the map.
    topo = _topology([], down={"grafana"})
    assert "grafana" not in _health(topo)
    assert set(_health(topo).values()) == {"healthy"}
