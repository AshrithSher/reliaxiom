"""Live IO adapters: build real pollers backed by `docker` subprocess calls. These are the
thin integration glue (verified against the running lab); the poller logic they feed is
unit-tested in test_pollers.py. Not unit-tested here — exercised live."""
from __future__ import annotations

import subprocess

from sre_agent.config import Config
from sre_agent.poll.pollers import (
    DockerInspectPoller,
    HealthPoller,
    Poller,
    PostgresConnPoller,
    RedisQueuePoller,
)

_HEALTH_PY = ("import urllib.request;"
              "print(urllib.request.urlopen('http://localhost:5000/health').status)")


def _run(cmd: list[str], timeout: float, cwd: str | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, check=True).stdout


def build_live_pollers(cfg: Config) -> list[Poller]:
    t = cfg.poll_timeout_s

    def docker_inspect() -> str:
        ids = _run(["docker", "ps", "-aq"], t, cfg.compose_dir).split()
        return _run(["docker", "inspect", *ids], t) if ids else "[]"

    def redis_depth() -> int:
        return int(_run(["docker", "exec", "redis", "redis-cli", "llen", "jobs"], t).strip())

    def pg_conns() -> int:
        return int(_run(["docker", "exec", "postgres", "psql", "-U", "demo", "-d", "demo",
                         "-tAc", "select count(*) from pg_stat_activity"], t).strip())

    def health(service: str) -> bool:
        return _run(["docker", "exec", service, "python", "-c", _HEALTH_PY], t).strip() == "200"

    return [
        DockerInspectPoller(run=docker_inspect),
        HealthPoller(services=cfg.health_services, probe=health),
        RedisQueuePoller(probe=redis_depth),
        PostgresConnPoller(probe=pg_conns),
    ]
