"""ROI metrics for the dashboard — the business-value numbers, computed from the post-mortem
archive (closed incidents) plus the live active incidents. Pure aggregation, no I/O, so it is
unit-tested deterministically. Mirrors the semantics of `health_report`
(`sre_agent/integrations/postmortems.py`) but returns structured data for the UI to render.
"""
from __future__ import annotations

from sre_agent.incident.models import Incident
from sre_agent.integrations.postmortems import PostMortem


def compute_metrics(postmortems: list[PostMortem], active: list[Incident]) -> dict:
    """Aggregate the agent's effectiveness.

    - `closed`   = incidents with a post-mortem (resolved or escalated)
    - `resolved` = the agent fixed it (no human) — these are the pages avoided
    - `escalated`= handed to a human
    - `mttr_s`   = mean time-to-resolve over resolved incidents (escalated have no TTR)
    """
    closed = len(postmortems)
    active_n = len(active)
    resolved = sum(1 for p in postmortems if p.outcome == "resolved")
    escalated = closed - resolved

    by_fault_type: dict[str, int] = {}
    by_service: dict[str, int] = {}
    by_fingerprint: dict[str, int] = {}
    for p in postmortems:
        fault = p.fingerprint.split(":", 1)[1] if ":" in p.fingerprint else p.fingerprint
        by_fault_type[fault] = by_fault_type.get(fault, 0) + 1
        by_fingerprint[p.fingerprint] = by_fingerprint.get(p.fingerprint, 0) + 1
        for svc in p.services:
            by_service[svc] = by_service.get(svc, 0) + 1

    recurring = sorted(([fp, n] for fp, n in by_fingerprint.items() if n > 1),
                       key=lambda x: -x[1])

    ttrs = [p.time_to_resolve_s for p in postmortems
            if p.outcome == "resolved" and p.time_to_resolve_s is not None]
    mttr = sum(ttrs) / len(ttrs) if ttrs else None
    fastest = min(ttrs) if ttrs else None

    trend = sorted(
        ({"incident_id": p.incident_id, "ttr_s": p.time_to_resolve_s,
          "created_at": p.created_at.isoformat()}
         for p in postmortems
         if p.outcome == "resolved" and p.time_to_resolve_s is not None),
        key=lambda t: t["created_at"])

    return {
        "total": closed + active_n,
        "closed": closed,
        "active": active_n,
        "resolved": resolved,
        "escalated": escalated,
        "auto_resolve_rate": resolved / closed if closed else 0.0,
        "pages_avoided": resolved,
        "mttr_s": mttr,
        "fastest_resolve_s": fastest,
        "by_fault_type": by_fault_type,
        "by_service": by_service,
        "recurring": recurring,
        "mttr_trend": trend,
    }
