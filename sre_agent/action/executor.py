"""Action executor + guardrails for Tier-1 AUTO remediation.

The executor is a thin orchestrator over a swappable `ActionBackend` (P0.1): it gates on
dry-run/shadow and backend capability, then delegates the actual primitive to the backend.
The substrate (docker on the lab, the Kubernetes API in production) lives entirely behind
the backend, never in the engine. Guardrails are enforced in code before anything runs:
only valid AUTO actions auto-execute, and no action ever touches postgres data. The
per-service restart *cap* is enforced separately and atomically by an `ActionRateLimiter`
(see `ratelimit.py`) so it holds across processes/replicas, not per-process.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from sre_agent.action.backend import ActionBackend, DockerActionBackend
from sre_agent.action.catalog import ResolvedAction, Tier


class ActionResult(BaseModel):
    action_id: str
    params: dict
    executed: bool
    success: bool
    detail: str = ""
    dry_run: bool = False


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


class Guardrails:
    """Code-enforced, non-rate gates: the action must be valid and AUTO-tier to auto-execute.
    The restart *cap* lives in the ActionRateLimiter (atomic + fleet-safe), not here."""

    def check(self, action: ResolvedAction, now: datetime) -> Decision:
        if not action.valid:
            return Decision(False, f"invalid action: {action.reason}")
        if action.tier is not Tier.AUTO:
            return Decision(False, f"tier {action.tier.name} is not auto-executable")
        return Decision(True)


class ActionExecutor:
    def __init__(self, backend: ActionBackend | None = None, dry_run: bool = True) -> None:
        self._backend = backend or DockerActionBackend()
        self._dry_run = dry_run

    def execute(self, action: ResolvedAction, now: datetime) -> ActionResult:
        if not self._backend.supports(action.action_id):
            return ActionResult(action_id=action.action_id, params=action.params,
                                executed=False, success=False,
                                detail=f"backend does not support {action.action_id}")
        if self._dry_run:
            return ActionResult(action_id=action.action_id, params=action.params,
                                executed=False, success=True, dry_run=True,
                                detail=f"dry-run: would {action.action_id} {action.params}")
        result = self._backend.apply(action)
        return ActionResult(action_id=action.action_id, params=action.params,
                            executed=result.executed, success=result.success,
                            detail=result.detail)
