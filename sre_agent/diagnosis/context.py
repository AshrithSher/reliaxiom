"""Context assembler — turns an incident + the log window into the (system, user) prompt,
in the D-012 priority order (correlated traces > error window > neighbor logs > topology)
under a character budget. Also builds the evidence corpus + verifier the diagnoser uses to
catch hallucinated citations.

Change-log and post-mortem-memory sections are placeholders until M5/M6.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Callable

from sre_agent.action.catalog import catalog_summary
from sre_agent.changelog import ChangeLog
from sre_agent.config import Config
from sre_agent.incident.models import Incident
from sre_agent.incident.topology import TopologyMap
from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import LogRecord, SignalSnapshot

_TOKEN = re.compile(r"[A-Za-z0-9_-]+")


def _significant(token: str) -> bool:
    """A token specific enough to anchor a citation: an id-like token (has a digit) or a
    long token (error codes like 'redis_unreachable'). Excludes common log words —
    'request', 'status', 'INFO' — so the model can't 'verify' by echoing boilerplate."""
    return any(ch.isdigit() for ch in token) or len(token) >= 8

_SYSTEM = """You are an SRE diagnostic assistant for a microservice system. You are given an \
incident and supporting logs. Identify the single most likely root cause and select exactly \
one remediation action from the catalog.

Action catalog (choose exactly one action id):
{catalog}

Respond with ONLY a JSON object (no prose, no markdown) with these keys:
  "root_cause": string,
  "evidence": [strings — cite real request_ids or exact log content from the logs below],
  "selected_action": one catalog action id, or "none" if no safe action applies,
  "action_params": object with that action's required params,
  "alternative_hypotheses": [strings].

Do NOT include a tier, severity, or confidence — the system decides those, not you. Cite \
only evidence that actually appears in the provided logs."""


class ContextAssembler:
    def __init__(self, topology: TopologyMap, cfg: Config,
                 changelog: ChangeLog | None = None, postmortems=None) -> None:
        self.topology = topology
        self.cfg = cfg
        self.changelog = changelog
        self.postmortems = postmortems

    def build(self, incident: Incident, window: SlidingWindow, now: datetime,
              signals: SignalSnapshot | None = None) -> tuple[str, str]:
        system = _SYSTEM.format(catalog=catalog_summary())
        records = self._gather(incident, window, now)
        # incident memory goes high (right after the header): "have we seen this before, and
        # what fixed it?" is the strongest prior a diagnostician has. Other context follows
        # the D-012 priority order: traces > change log > live signals > error window > neighbors.
        sections = [
            self._incident_header(incident),
            self._memory_section(incident),
            self._traces(records),
            self._changelog_section(incident),
            self._signals_section(signals),
            self._error_window(incident, records),
            self._neighbor_logs(incident, records),
            self._topology_text(incident),
        ]
        return system, self._budget(sections)

    # --- evidence verification ---------------------------------------------------
    def evidence_corpus(self, incident: Incident, window: SlidingWindow,
                        now: datetime) -> str:
        return "\n".join(_fmt(r) for r in self._gather(incident, window, now))

    def make_verifier(self, corpus: str) -> Callable[[str], bool]:
        tokens = {t for t in _TOKEN.findall(corpus) if _significant(t)}

        def verify(ref: str) -> bool:
            return any(_significant(t) and t in tokens for t in _TOKEN.findall(ref))

        return verify

    # --- internals ---------------------------------------------------------------
    def _relevant_services(self, incident: Incident) -> list[str]:
        services = set(incident.services) | {incident.root_service}
        neighbors: set[str] = set()
        for svc in incident.services:
            neighbors |= self.topology.dependencies_of(svc)
            neighbors |= self.topology.dependents_of(svc)
        return list(services | neighbors)

    def _gather(self, incident: Incident, window: SlidingWindow,
                now: datetime) -> list[LogRecord]:
        since = now - timedelta(seconds=self.cfg.diagnosis_window_s)
        out: list[LogRecord] = []
        for svc in self._relevant_services(incident):
            out.extend(window.records(svc, since))
        out.sort(key=lambda r: r.ts)
        return out

    @staticmethod
    def _incident_header(incident: Incident) -> str:
        return (f"## INCIDENT {incident.fingerprint}\n"
                f"root_service={incident.root_service} services={incident.services} "
                f"fault={incident.fault_type} severity={incident.severity} "
                f"first_seen={incident.first_seen.isoformat()}")

    def _traces(self, records: list[LogRecord]) -> str:
        by_rid: dict[str, list[LogRecord]] = {}
        for r in records:
            if r.request_id:
                by_rid.setdefault(r.request_id, []).append(r)
        lines = ["## CORRELATED REQUEST TRACES"]
        for rid, group in by_rid.items():
            services = {r.service for r in group}
            has_error = any(r.is_error for r in group)
            if len(services) > 1 or has_error:   # cross-service or failing — the signal
                lines.append(f"request {rid}:")
                lines.extend("  " + _fmt(r) for r in sorted(group, key=lambda r: r.ts))
        return "\n".join(lines) if len(lines) > 1 else ""

    def _error_window(self, incident: Incident, records: list[LogRecord]) -> str:
        affected = set(incident.services)
        errs = [r for r in records if r.service in affected and r.is_error]
        if not errs:
            return ""
        return "## ERROR WINDOW (affected services)\n" + "\n".join(_fmt(r) for r in errs[-20:])

    def _neighbor_logs(self, incident: Incident, records: list[LogRecord]) -> str:
        affected = set(incident.services) | {incident.root_service}
        neigh = [r for r in records if r.service not in affected]
        if not neigh:
            return ""
        return "## NEIGHBOR LOGS\n" + "\n".join(_fmt(r) for r in neigh[-15:])

    def _memory_section(self, incident: Incident) -> str:
        if self.postmortems is None:
            return ""
        past = self.postmortems.recent_for_fingerprint(incident.fingerprint, limit=3)
        if not past:
            return ""
        lines = ["## INCIDENT MEMORY (this fingerprint has occurred before — prefer a proven fix)"]
        for p in past:
            ttr = f" (resolved in {p.time_to_resolve_s:.0f}s)" if p.time_to_resolve_s else ""
            lines.append(f"- {p.created_at.date().isoformat()}: root cause \"{p.root_cause}\" "
                         f"→ {p.action_taken} → {p.outcome}{ttr}")
        return "\n".join(lines)

    def _changelog_section(self, incident: Incident) -> str:
        if self.changelog is None:
            return ""
        since = incident.first_seen - timedelta(seconds=self.cfg.diagnosis_window_s)
        entries = self.changelog.recent(since, incident.first_seen + timedelta(seconds=5))
        if not entries:
            return ""
        lines = "\n".join(f"{e.ts.isoformat()} {e.actor} {e.change_type} {e.service}: {e.detail}"
                          for e in entries)
        return "## RECENT CHANGES (before symptom onset)\n" + lines

    def _signals_section(self, signals: SignalSnapshot | None) -> str:
        if signals is None:
            return ""
        lines = ["## LIVE SIGNALS (read-only: docker/health/redis/postgres)"]
        for name, c in signals.containers.items():
            lines.append(f"container {name}: status={c.status} restarts={c.restart_count}")
        if signals.health:
            lines.append("health: " + ", ".join(f"{s}={'ok' if ok else 'DOWN'}"
                                                 for s, ok in signals.health.items()))
        if signals.redis_queue_depth is not None:
            lines.append(f"redis queue_depth={signals.redis_queue_depth}")
        if signals.pg_connections is not None:
            lines.append(f"postgres connections={signals.pg_connections}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def _topology_text(self, incident: Incident) -> str:
        deps = self.topology.dependencies_of(incident.root_service)
        return (f"## TOPOLOGY\n{incident.root_service} depends on {sorted(deps) or 'nothing'}; "
                f"affected services: {incident.services}")

    def _budget(self, sections: list[str]) -> str:
        budget = self.cfg.diagnosis_budget_chars
        out = ""
        for part in sections:
            if not part:
                continue
            if len(out) + len(part) + 1 > budget:
                out += part[: max(0, budget - len(out))]
                break
            out += part + "\n"
        return out[:budget]


def _fmt(r: LogRecord) -> str:
    parts = [r.ts.isoformat(), r.service, r.level]
    if r.event:
        parts.append(r.event)
    if r.status is not None:
        parts.append(f"status={r.status}")
    if r.request_id:
        parts.append(f"req={r.request_id}")
    err = r.raw.get("error")
    if err:
        parts.append(f"error={err}")
    return " ".join(parts)
