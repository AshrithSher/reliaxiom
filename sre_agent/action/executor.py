"""Action executor + guardrails for Tier-1 AUTO remediation.

The executor maps a resolved catalog action to a Docker command and runs it (IO injected,
so it's testable offline). Dry-run logs intent without executing. Guardrails are enforced in
code before anything runs: only AUTO actions auto-execute, restarts are capped per service
per hour (counted from the change log), and no action ever touches postgres data.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from pydantic import BaseModel

from sre_agent.action.catalog import ResolvedAction, Tier
from sre_agent.changelog import ChangeLog

# runner(cmd) -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], tuple[int, str, str]]


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
    def __init__(self, max_restarts_per_hour: int, changelog: ChangeLog) -> None:
        self._max_restarts = max_restarts_per_hour
        self._changelog = changelog

    def check(self, action: ResolvedAction, now: datetime) -> Decision:
        if not action.valid:
            return Decision(False, f"invalid action: {action.reason}")
        if action.tier is not Tier.AUTO:
            return Decision(False, f"tier {action.tier.name} is not auto-executable")
        if action.action_id == "restart_container":
            service = action.params["service"]
            recent = self._changelog.recent(now - timedelta(hours=1), now)
            count = sum(1 for e in recent if e.actor == "sre-agent"
                        and e.change_type == "restart_container" and e.service == service)
            if count >= self._max_restarts:
                return Decision(False, f"restart cap reached for {service} "
                                       f"({count}/{self._max_restarts} in last hour)")
        return Decision(True)


class ActionExecutor:
    def __init__(self, runner: Runner | None = None, dry_run: bool = True) -> None:
        self._runner = runner or _docker_runner
        self._dry_run = dry_run

    def execute(self, action: ResolvedAction, now: datetime) -> ActionResult:
        cmd = self._command(action)
        if cmd is None:
            return ActionResult(action_id=action.action_id, params=action.params,
                                executed=False, success=False,
                                detail=f"no executor mapping for {action.action_id}")
        if self._dry_run:
            return ActionResult(action_id=action.action_id, params=action.params,
                                executed=False, success=True, dry_run=True,
                                detail=f"dry-run: would run `{' '.join(cmd)}`")
        rc, out, err = self._runner(cmd)
        detail = (out if rc == 0 else err) or out or err or ""
        return ActionResult(action_id=action.action_id, params=action.params,
                            executed=True, success=(rc == 0), detail=detail.strip())

    @staticmethod
    def _command(action: ResolvedAction) -> list[str] | None:
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
