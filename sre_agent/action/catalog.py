"""The enumerated action catalog. The LLM selects an action *id* from this catalog and
nothing more; the risk TIER is resolved here in code (invariant #2). Unknown action or
invalid params => escalate. restart_container's tier depends on whether the target is
stateless (auto) or stateful (approval) — never destructive to postgres data (invariant #4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Tier(IntEnum):
    AUTO = 1          # agent acts immediately (under guardrails)
    APPROVAL = 2      # human must approve
    ESCALATE = 3      # agent does not act; hand to a human


STATELESS_SERVICES = {"webapp", "api", "worker", "gateway", "auth", "payments", "loadgen"}
STATEFUL_SERVICES = {"redis", "postgres"}


@dataclass(frozen=True)
class ActionSpec:
    id: str
    base_tier: Tier
    required_params: tuple[str, ...]
    description: str


CATALOG: dict[str, ActionSpec] = {
    "restart_container": ActionSpec(
        "restart_container", Tier.AUTO, ("service",),
        "Restart a container. Stateless services are tier 1; redis/postgres are tier 2."),
    "clear_stuck_queue_item": ActionSpec(
        "clear_stuck_queue_item", Tier.AUTO, ("item_id",),
        "Remove a single stuck item from the job queue."),
    "rerun_failed_job": ActionSpec(
        "rerun_failed_job", Tier.AUTO, ("job_id",),
        "Re-enqueue a single failed (idempotent) job."),
    "flush_queue": ActionSpec(
        "flush_queue", Tier.APPROVAL, (),
        "Flush the job queue. Possible data loss — approval required."),
    "change_config": ActionSpec(
        "change_config", Tier.APPROVAL, ("service", "key", "value"),
        "Change a whitelisted config key on a service."),
}


@dataclass
class ResolvedAction:
    action_id: str
    params: dict
    tier: Tier
    valid: bool
    reason: str = ""


def resolve(action_id: str, params: dict | None = None) -> ResolvedAction:
    params = params or {}
    spec = CATALOG.get(action_id)
    if spec is None:
        return ResolvedAction(action_id, params, Tier.ESCALATE, False,
                              f"unknown action '{action_id}' — not in catalog")
    missing = [p for p in spec.required_params if p not in params]
    if missing:
        return ResolvedAction(action_id, params, Tier.ESCALATE, False,
                              f"missing required params {missing}")
    if action_id == "restart_container":
        service = params["service"]
        if service in STATELESS_SERVICES:
            return ResolvedAction(action_id, params, Tier.AUTO, True)
        if service in STATEFUL_SERVICES:
            return ResolvedAction(action_id, params, Tier.APPROVAL, True)
        return ResolvedAction(action_id, params, Tier.ESCALATE, False,
                              f"unknown service '{service}'")
    return ResolvedAction(action_id, params, spec.base_tier, True)


def catalog_summary() -> str:
    """One line per action — embedded in the diagnosis prompt so the model picks a real id."""
    return "\n".join(
        f"- {spec.id}: {spec.description} "
        f"(required params: {', '.join(spec.required_params) or 'none'})"
        for spec in CATALOG.values()
    )
