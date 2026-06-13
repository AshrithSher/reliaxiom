"""TDD: secondary-signal pollers. Each polls one external source on an interval and
shapes the result into the SignalSnapshot. All IO is injected, so failures are exercised
without real Docker/Redis: a poller that can't reach its source must degrade gracefully
(None / unhealthy), never crash the loop."""
from datetime import datetime, timezone

from sre_agent.poll.pollers import (
    DockerInspectPoller,
    HealthPoller,
    PostgresConnPoller,
    RedisQueuePoller,
)

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)

INSPECT_JSON = """[
  {"Name":"/api","State":{"Status":"running"},"RestartCount":0},
  {"Name":"/worker","State":{"Status":"restarting"},"RestartCount":7},
  {"Name":"/postgres","State":{"Status":"exited"},"RestartCount":1}
]"""


# --- DockerInspectPoller ---------------------------------------------------------
def test_docker_poller_parses_status_and_restart_counts():
    poller = DockerInspectPoller(run=lambda: INSPECT_JSON)
    out = poller.poll()["containers"]
    assert out["api"].status == "running" and out["api"].restart_count == 0
    assert out["worker"].status == "restarting" and out["worker"].restart_count == 7
    assert out["postgres"].status == "exited"
    assert out["api"].healthy and not out["worker"].healthy


def test_docker_poller_strips_leading_slash():
    poller = DockerInspectPoller(run=lambda: INSPECT_JSON)
    assert "api" in poller.poll()["containers"]
    assert "/api" not in poller.poll()["containers"]


def test_docker_poller_handles_empty():
    assert DockerInspectPoller(run=lambda: "[]").poll() == {"containers": {}}


def test_docker_poller_survives_garbage_output():
    poller = DockerInspectPoller(run=lambda: "Cannot connect to the Docker daemon")
    assert poller.poll() == {"containers": {}}  # no crash, no data


def test_docker_poller_survives_runner_raising():
    def boom():
        raise OSError("docker not found")
    assert DockerInspectPoller(run=boom).poll() == {"containers": {}}


# --- HealthPoller ----------------------------------------------------------------
def test_health_poller_reports_per_service():
    probe = lambda svc: svc != "api"  # api is down, others up
    out = HealthPoller(services=["api", "webapp"], probe=probe).poll()["health"]
    assert out == {"api": False, "webapp": True}


def test_health_poller_treats_probe_error_as_unhealthy():
    def probe(svc):
        raise ConnectionError("refused")
    assert HealthPoller(services=["api"], probe=probe).poll()["health"] == {"api": False}


# --- RedisQueuePoller / PostgresConnPoller --------------------------------------
def test_redis_queue_poller_reports_depth():
    assert RedisQueuePoller(probe=lambda: 42).poll() == {"redis_queue_depth": 42}


def test_redis_queue_poller_none_on_failure():
    def boom():
        raise ConnectionError("redis down")
    assert RedisQueuePoller(probe=boom).poll() == {"redis_queue_depth": None}


def test_pg_conn_poller_reports_count():
    assert PostgresConnPoller(probe=lambda: 12).poll() == {"pg_connections": 12}


def test_pg_conn_poller_none_on_failure():
    assert PostgresConnPoller(probe=lambda: (_ for _ in ()).throw(OSError())).poll() \
        == {"pg_connections": None}
