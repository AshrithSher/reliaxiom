"""The lab dependency graph and the queries correlation needs.

`depends_on[X]` lists the services X directly relies on. "Upstream of X" means something X
depends on (its root cause candidates); "dependents of X" are the services that would
visibly break if X fails."""
from __future__ import annotations


class TopologyMap:
    def __init__(self, depends_on: dict[str, list[str]]) -> None:
        self._direct = {svc: set(deps) for svc, deps in depends_on.items()}
        # ensure every referenced service is a key
        for deps in list(self._direct.values()):
            for dep in deps:
                self._direct.setdefault(dep, set())

    def dependencies_of(self, service: str) -> set[str]:
        """Everything `service` transitively relies on."""
        return self._reachable(service, self._direct)

    def dependents_of(self, service: str) -> set[str]:
        """Everything that transitively relies on `service`."""
        return self._reachable(service, self._reverse())

    def is_upstream_of(self, a: str, b: str) -> bool:
        """True if b depends on a (a is a root-cause candidate for b)."""
        return a in self.dependencies_of(b)

    def most_upstream(self, services: set[str]) -> str | None:
        """The single member of `services` that every other member depends on, if any.
        In a DAG at most one such service exists."""
        for candidate in services:
            others = services - {candidate}
            if others and all(self.is_upstream_of(candidate, other) for other in others):
                return candidate
        return None

    def common_dependencies(self, services: set[str]) -> set[str]:
        deps = [self.dependencies_of(s) for s in services]
        return set.intersection(*deps) if deps else set()

    def _reverse(self) -> dict[str, set[str]]:
        rev: dict[str, set[str]] = {svc: set() for svc in self._direct}
        for svc, deps in self._direct.items():
            for dep in deps:
                rev[dep].add(svc)
        return rev

    @staticmethod
    def _reachable(start: str, graph: dict[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        stack = list(graph.get(start, set()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, set()))
        return seen


# loadgen → gateway → webapp → api → {postgres, redis, auth};
# worker → {redis, postgres, payments}.  auth and payments are leaf dependencies (like
# postgres/redis): the api authenticates every request via auth, and the worker charges
# every order via payments, so an outage of either fans out to its consumers.
LAB_TOPOLOGY = TopologyMap({
    "loadgen": ["gateway"],
    "gateway": ["webapp"],
    "webapp": ["api"],
    "api": ["postgres", "redis", "auth"],
    "worker": ["redis", "postgres", "payments"],
    "auth": [],
    "payments": [],
    "postgres": [],
    "redis": [],
})
