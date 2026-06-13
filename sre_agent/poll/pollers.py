"""Secondary-signal pollers. Each shapes one external source into a partial SignalSnapshot
dict. IO is injected (a `run`/`probe` callable) so logic is testable without real services
and so a failing source degrades to None/unhealthy instead of raising."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Callable

from sre_agent.models import ContainerState


class Poller(ABC):
    @abstractmethod
    def poll(self) -> dict[str, Any]:
        """Return partial SignalSnapshot fields. Must not raise — failures degrade."""


class DockerInspectPoller(Poller):
    """Parses `docker inspect` JSON (one object per container) into ContainerStates,
    carrying status and restart count. Garbage output or a runner error → no data."""

    def __init__(self, run: Callable[[], str]) -> None:
        self._run = run

    def poll(self) -> dict[str, Any]:
        containers: dict[str, ContainerState] = {}
        try:
            raw = self._run()
            items = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return {"containers": {}}
        if not isinstance(items, list):
            return {"containers": {}}
        for item in items:
            try:
                name = str(item["Name"]).lstrip("/")
                containers[name] = ContainerState(
                    name=name,
                    status=str(item["State"]["Status"]),
                    restart_count=int(item.get("RestartCount", 0)),
                )
            except (KeyError, TypeError, ValueError):
                continue  # skip a malformed entry, keep the rest
        return {"containers": containers}


class HealthPoller(Poller):
    """Per-service health probe. A probe that raises means unreachable → unhealthy."""

    def __init__(self, services: list[str], probe: Callable[[str], bool]) -> None:
        self._services = services
        self._probe = probe

    def poll(self) -> dict[str, Any]:
        health: dict[str, bool] = {}
        for service in self._services:
            try:
                health[service] = bool(self._probe(service))
            except Exception:
                health[service] = False
        return {"health": health}


class RedisQueuePoller(Poller):
    """Redis job-queue depth — visible even when the worker is silent (kill-worker)."""

    def __init__(self, probe: Callable[[], int]) -> None:
        self._probe = probe

    def poll(self) -> dict[str, Any]:
        try:
            return {"redis_queue_depth": int(self._probe())}
        except Exception:
            return {"redis_queue_depth": None}


class PostgresConnPoller(Poller):
    """Postgres active connection count — saturation/leak signal."""

    def __init__(self, probe: Callable[[], int]) -> None:
        self._probe = probe

    def poll(self) -> dict[str, Any]:
        try:
            return {"pg_connections": int(self._probe())}
        except Exception:
            return {"pg_connections": None}
