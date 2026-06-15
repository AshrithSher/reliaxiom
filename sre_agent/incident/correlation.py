"""Correlate Incident Candidates into incidents — collapse a multi-symptom fault into one,
attributed to its most upstream / most-implicated suspect. This is the heart of the
'one fault = one ticket' invariant (#3).

P1.1 / D-040 — a multi-signal correlation engine, not a priority cascade. The earlier design
keyed only on dependency error codes then topology, which left one fault's *heterogeneous*
symptoms as separate tickets: a memory leak trips crash_loop AND error_rate on the same
service (no shared error code, different signal types); added latency trips latency_p95 AND
error_rate; the log, metric, and trace twins of one fault land separately. We now build an
**affinity graph** over candidates and take its connected components — one component, one
incident — with edges across signal modalities:

  E1  same service                 — heterogeneous signals on one service are one fault
  E2  shared trace/request id      — the strongest CAUSAL signal: the same failing requests
                                      crossing services (bypasses the time window)
  E3  directional topology chain   — A is upstream of B (a real cascade), NOT merely a shared
                                      dependency (D-016: worker+gateway share redis/postgres
                                      but neither is upstream of the other — never fused on
                                      that alone; a genuine shared-dep outage announces itself
                                      with an error code, which is E4)
  E4  dependency-error co-attribution — two candidates that both depend on the SAME implicated
                                      root (some candidate carried its error code)

Non-causal edges (E1/E3/E4) are gated by an adaptive window (correlation_max_span_s); E2 is
not — a shared trace is causal whenever it occurs.

Root attribution within a component, in precedence order:
  1. dependency error code (redis_unreachable → redis) — the most precise, evidence-driven
  2. a recently-changed service (deploy/config coincidence) present in the component
  3. topology: the service every other member depends on (most_upstream)
  4. the member with the most dependents (closest to a shared root), then earliest first_seen
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from sre_agent.incident.topology import TopologyMap
from sre_agent.models import IncidentCandidate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

# error code → (root service, fault_type)
DEP_ERROR_ROOTS: dict[str, tuple[str, str]] = {
    "redis_unreachable": ("redis", "unreachable"),
    "db_unreachable": ("postgres", "unreachable"),
    "auth_unreachable": ("auth", "unreachable"),
    "payments_unreachable": ("payments", "unreachable"),
}


class ChangeEvent(Protocol):
    """The slice of a change-log entry correlation needs (duck-typed ChangeLogEntry)."""
    service: str
    ts: datetime
    actor: str


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
                 dep_error_roots: dict[str, tuple[str, str]] | None = None,
                 max_span_s: float = 300.0, change_window_s: float = 600.0) -> None:
        self.topology = topology
        self.dep_error_roots = dep_error_roots if dep_error_roots is not None else DEP_ERROR_ROOTS
        self.max_span = timedelta(seconds=max_span_s)
        self.change_window = timedelta(seconds=change_window_s)

    def correlate(self, candidates: list[IncidentCandidate],
                  change_events: "Sequence[ChangeEvent] | None" = None,
                  now: datetime | None = None) -> list[CorrelationGroup]:
        if not candidates:
            return []
        if now is None:
            now = max(c.confirmed_at for c in candidates)

        # implicated dependency roots: a root R is "implicated" when some candidate carries
        # its error code. Used for E4 (co-attribution) and for root precedence #1.
        implicated = self._implicated_roots(candidates)
        # per-candidate set of implicated roots it depends on (or is) — the E4 attribution
        attribs = [self._attributed_roots(c, implicated) for c in candidates]

        # --- build the affinity graph via union-find ---------------------------------
        parent = list(range(len(candidates)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            parent[find(i)] = find(j)

        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                if self._linked(candidates[i], candidates[j], attribs[i], attribs[j]):
                    union(i, j)

        # --- collect components ------------------------------------------------------
        components: dict[int, list[int]] = {}
        for i in range(len(candidates)):
            components.setdefault(find(i), []).append(i)

        groups: list[CorrelationGroup] = []
        for members_idx in components.values():
            members = [candidates[i] for i in members_idx]
            member_implicated = {r for i in members_idx for r in attribs[i]}
            root, fault = self._attribute(members, member_implicated, implicated,
                                          change_events, now)
            groups.append(self._group(root, fault, members))
        return groups

    # --- edges -------------------------------------------------------------------
    def _linked(self, a: IncidentCandidate, b: IncidentCandidate,
                a_roots: set[str], b_roots: set[str]) -> bool:
        # E2 shared trace id — causal regardless of timing, so it bypasses the window
        if set(a.trace_ids) & set(b.trace_ids):
            return True
        if abs(a.confirmed_at - b.confirmed_at) > self.max_span:
            return False
        # E1 same service
        if set(a.services) & set(b.services):
            return True
        # E3 directional topology chain (a real cascade, not a shared dependency — D-016)
        if self._chained(a, b):
            return True
        # E4 dependency-error co-attribution (both depend on the same implicated root)
        if a_roots & b_roots:
            return True
        return False

    def _chained(self, a: IncidentCandidate, b: IncidentCandidate) -> bool:
        return any(self.topology.is_upstream_of(sa, sb) or self.topology.is_upstream_of(sb, sa)
                   for sa in a.services for sb in b.services)

    # --- attribution -------------------------------------------------------------
    def _attribute(self, members: list[IncidentCandidate], member_implicated: set[str],
                   implicated: dict[str, tuple[str, int]],
                   change_events: "Sequence[ChangeEvent] | None",
                   now: datetime) -> tuple[str, str]:
        services = self._member_services(members)

        # 1. dependency error code — most precise, evidence-driven. Most-evidenced root wins.
        if member_implicated:
            root = min(member_implicated, key=lambda r: (-implicated[r][1], r))
            return root, implicated[root][0]

        # 2. a recently-changed service present in the component (deploy/config coincidence)
        changed = self._changed_root(set(services), change_events, now)
        if changed is not None:
            return changed, self._fault_of(self._root_candidate(members, changed))

        # 3. topology: the service every other member depends on
        root = self.topology.most_upstream(set(services)) if len(services) > 1 else None
        if root is not None:
            return root, self._fault_of(self._root_candidate(members, root))

        # 4. the member closest to a shared root (most dependents), then earliest, then name
        root = min(services, key=lambda s: (
            -len(self.topology.dependents_of(s)),
            self._first_seen_of(members, s),
            s,
        ))
        return root, self._fault_of(self._root_candidate(members, root))

    def _implicated_roots(self, candidates: list[IncidentCandidate]) -> dict[str, tuple[str, int]]:
        implicated: dict[str, tuple[str, int]] = {}  # root -> (fault_type, mentions)
        for c in candidates:
            for code in c.error_codes:
                mapped = self.dep_error_roots.get(code)
                if mapped:
                    root, fault = mapped
                    _, mentions = implicated.get(root, (fault, 0))
                    implicated[root] = (fault, mentions + 1)
        return implicated

    def _attributed_roots(self, c: IncidentCandidate,
                          implicated: dict[str, tuple[str, int]]) -> set[str]:
        """The implicated roots this candidate is or depends on — its E4 attribution."""
        return {r for r in implicated if self._depends_on(c, r)}

    def _changed_root(self, services: set[str],
                      change_events: "Sequence[ChangeEvent] | None",
                      now: datetime) -> str | None:
        if not change_events:
            return None
        cutoff = now - self.change_window
        coincident = [e for e in change_events
                      if e.actor != "sre-agent" and e.service in services
                      and cutoff <= e.ts <= now]
        if not coincident:
            return None
        return max(coincident, key=lambda e: e.ts).service

    def _depends_on(self, c: IncidentCandidate, root: str) -> bool:
        return any(s == root or self.topology.is_upstream_of(root, s) for s in c.services)

    @staticmethod
    def _first_seen_of(members: list[IncidentCandidate], service: str) -> datetime:
        seen = [c.first_seen for c in members if service in c.services]
        return min(seen) if seen else max(c.first_seen for c in members)

    @staticmethod
    def _root_candidate(members: list[IncidentCandidate], root: str) -> IncidentCandidate:
        for c in members:
            if root in c.services:
                return c
        return min(members, key=lambda c: c.first_seen)

    @staticmethod
    def _fault_of(c: IncidentCandidate) -> str:
        return c.error_codes[0] if c.error_codes else c.signal_type

    @staticmethod
    def _member_services(members: list[IncidentCandidate]) -> list[str]:
        services: list[str] = []
        for c in members:
            for s in c.services:
                if s not in services:
                    services.append(s)
        return services

    def _group(self, root: str, fault: str,
               members: list[IncidentCandidate]) -> CorrelationGroup:
        return CorrelationGroup(root_service=root, fault_type=fault,
                                services=self._member_services(members), candidates=members)
