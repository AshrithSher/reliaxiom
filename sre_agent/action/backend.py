"""The ActionBackend SPI — the swappable execution seam (ROADMAP P0.1, invariant #2).

The engine selects an action *id* from the enumerated catalog; a backend translates that
abstract action into a concrete primitive on whatever substrate it owns (docker on the lab,
the Kubernetes API in production) and reports the live state of the target. The catalog stays
the single source of truth: a backend only *declares which ids it supports*, and an
unsupported id is inert — the seam MCP-backed action servers will later plug into.

All backends follow the D-015 discipline used across the integration layer: IO is injected
(so logic is unit-tested offline), and reads (`observe`) catch all transport/parse failures
and return None rather than raising or fabricating a state. A missing reading is honest; a
wrong one is a lie the verification loop would later trust.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable, Protocol, runtime_checkable

from pydantic import BaseModel

from sre_agent.action.catalog import ResolvedAction

# runner(argv) -> (returncode, stdout, stderr). Injected so docker calls are testable offline.
DockerRunner = Callable[[list[str]], tuple[int, str, str]]


class TargetState(BaseModel):
    """A backend's live read of an action's target — the basis for both the idempotency
    pre-check (don't re-act on something already in the desired state) and the post-action
    verification (did reality actually change), instead of trusting a command exit code."""

    exists: bool
    healthy: bool
    detail: str = ""


class BackendResult(BaseModel):
    executed: bool
    success: bool
    detail: str = ""


@runtime_checkable
class ActionBackend(Protocol):
    def supports(self, action_id: str) -> bool:
        """True iff this backend can perform the action. Unsupported ids are inert (#2)."""

    def observe(self, action: ResolvedAction) -> TargetState | None:
        """Live state of the action's target, or None if unobservable (degrade, D-015)."""

    def apply(self, action: ResolvedAction) -> BackendResult:
        """Perform the action. Idempotent / declarative where the substrate allows."""


_DOCKER_ACTIONS = {"restart_container", "clear_stuck_queue_item", "rerun_failed_job"}


class DockerActionBackend:
    """The lab's default backend: catalog action -> docker CLI argv, run via an injected
    runner. This is the original ActionExecutor behavior, lifted behind the SPI unchanged so
    the engine no longer hard-codes the execution substrate."""

    def __init__(self, runner: DockerRunner | None = None) -> None:
        self._runner = runner or _docker_runner

    def supports(self, action_id: str) -> bool:
        return action_id in _DOCKER_ACTIONS

    def observe(self, action: ResolvedAction) -> TargetState | None:
        service = action.params.get("service")
        if not service:
            return None
        rc, out, err = self._runner(["docker", "inspect", service])
        if rc != 0 or not out.strip():
            # a definite "not there" (inspect failed) is a real reading, not a degrade
            return TargetState(exists=False, healthy=False, detail=(err or out).strip())
        try:
            items = json.loads(out)
            status = str(items[0]["State"]["Status"])
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            return None  # unparseable → no reading (never fabricate)
        return TargetState(exists=True, healthy=(status == "running"), detail=status)

    def apply(self, action: ResolvedAction) -> BackendResult:
        cmd = _docker_command(action)
        if cmd is None:
            return BackendResult(executed=False, success=False,
                                 detail=f"no docker mapping for {action.action_id}")
        rc, out, err = self._runner(cmd)
        detail = (out if rc == 0 else err) or out or err or ""
        return BackendResult(executed=True, success=(rc == 0), detail=detail.strip())


def _docker_command(action: ResolvedAction) -> list[str] | None:
    aid, p = action.action_id, action.params
    if aid == "restart_container":
        return ["docker", "restart", p["service"]]
    if aid == "clear_stuck_queue_item":
        return ["docker", "exec", "redis", "redis-cli", "lrem", "jobs", "0", p["item_id"]]
    if aid == "rerun_failed_job":
        return ["docker", "exec", "redis", "redis-cli", "rpush", "jobs", p["job_id"]]
    return None


def _docker_runner(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return proc.returncode, proc.stdout, proc.stderr
