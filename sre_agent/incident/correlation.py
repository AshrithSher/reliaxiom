"""Correlate Incident Candidates into incidents — collapse a multi-symptom fault into one,
attributed to its most upstream suspect (DECISIONS.md D-002, refined by D-016).

Attribution, in priority order:
  1. dependency error codes (redis_unreachable → redis) — precise and evidence-driven
  2. topology — among the rest, the service every other depends on is the root
  3. leftovers — each candidate becomes its own incident
Candidates that merely share a common dependency are NOT merged on that alone: worker and
gateway both depend on redis/postgres, so shared-dep merging would fuse every unrelated
blip between them. A real shared-dependency fault announces itself with an error code
(tier 1), which is the signal we key on.
"""
from __future__ import annotations

from pydantic import BaseModel

from sre_agent.incident.topology import TopologyMap
from sre_agent.models import IncidentCandidate

# error code → (root service, fault_type)
DEP_ERROR_ROOTS: dict[str, tuple[str, str]] = {
    "redis_unreachable": ("redis", "unreachable"),
    "db_unreachable": ("postgres", "unreachable"),
    "auth_unreachable": ("auth", "unreachable"),
    "payments_unreachable": ("payments", "unreachable"),
}


class CorrelationGroup(BaseModel):
    root_service: str
    fault_type: str
    services: list[str]
    candidates: list[IncidentCandidate]

    @property
    def fingerprint(self) -> str:
        return f"{self.root_service}:{self.fault_type}"


class Correlator:
    def __init__(self, topology: TopologyMap,
                 dep_error_roots: dict[str, tuple[str, str]] | None = None) -> None:
        self.topology = topology
        self.dep_error_roots = dep_error_roots if dep_error_roots is not None else DEP_ERROR_ROOTS

    def correlate(self, candidates: list[IncidentCandidate]) -> list[CorrelationGroup]:
        used: set[int] = set()
        groups: list[CorrelationGroup] = []

        # --- 1. dependency-error attribution -------------------------------------
        implicated: dict[str, tuple[str, int]] = {}  # root -> (fault_type, mentions)
        for c in candidates:
            for code in c.error_codes:
                mapped = self.dep_error_roots.get(code)
                if mapped:
                    root, fault = mapped
                    _, mentions = implicated.get(root, (fault, 0))
                    implicated[root] = (fault, mentions + 1)
        # most-evidenced root first, so a candidate depending on two implicated roots
        # joins the better-supported one
        for root in sorted(implicated, key=lambda r: (-implicated[r][1], r)):
            fault = implicated[root][0]
            members = [c for c in candidates
                       if id(c) not in used and self._depends_on(c, root)]
            if members:
                used.update(id(c) for c in members)
                groups.append(self._group(root, fault, members))

        # --- 2. topological attribution among the rest ---------------------------
        remaining = [c for c in candidates if id(c) not in used]
        services = {s for c in remaining for s in c.services}
        root = self.topology.most_upstream(services) if len(services) > 1 else None
        if root is not None:
            members = [c for c in remaining if self._depends_on(c, root)]
            if len(members) > 1:
                used.update(id(c) for c in members)
                fault = self._fault_of(self._root_candidate(members, root))
                groups.append(self._group(root, fault, members))

        # --- 3. leftovers --------------------------------------------------------
        for c in candidates:
            if id(c) in used:
                continue
            used.add(id(c))
            groups.append(self._group(c.services[0], self._fault_of(c), [c]))

        return groups

    def _depends_on(self, c: IncidentCandidate, root: str) -> bool:
        return any(s == root or self.topology.is_upstream_of(root, s) for s in c.services)

    @staticmethod
    def _root_candidate(members: list[IncidentCandidate], root: str) -> IncidentCandidate:
        for c in members:
            if root in c.services:
                return c
        return members[0]

    @staticmethod
    def _fault_of(c: IncidentCandidate) -> str:
        return c.error_codes[0] if c.error_codes else c.signal_type

    @staticmethod
    def _group(root: str, fault: str, members: list[IncidentCandidate]) -> CorrelationGroup:
        services: list[str] = []
        for c in members:
            for s in c.services:
                if s not in services:
                    services.append(s)
        return CorrelationGroup(root_service=root, fault_type=fault,
                                services=services, candidates=members)
