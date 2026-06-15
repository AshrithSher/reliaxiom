"""TDD: the action executor + guardrails. Tier-1 AUTO actions run against the lab via an
injected runner (so it's testable offline). Dry-run logs intent without executing. Guardrails
are code-enforced: only AUTO actions auto-execute, restarts are capped per service per hour,
and postgres data is never touched."""
from datetime import datetime, timezone

from sre_agent.action.backend import DockerActionBackend
from sre_agent.action.catalog import resolve
from sre_agent.action.executor import ActionExecutor, Guardrails

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeRunner:
    def __init__(self, rc=0, out="ok", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return self.rc, self.out, self.err


def _executor(runner, dry_run):
    """The executor now delegates to a backend; the docker runner is injected into it."""
    return ActionExecutor(backend=DockerActionBackend(runner=runner), dry_run=dry_run)


# --- executor --------------------------------------------------------------------
def test_dry_run_does_not_execute():
    runner = FakeRunner()
    ex = _executor(runner, dry_run=True)
    result = ex.execute(resolve("restart_container", {"service": "worker"}), now=NOW)
    assert result.dry_run and not result.executed and result.success
    assert "worker" in result.detail
    assert runner.calls == []          # nothing actually ran


def test_live_restart_runs_docker_restart():
    runner = FakeRunner()
    ex = _executor(runner, dry_run=False)
    result = ex.execute(resolve("restart_container", {"service": "worker"}), now=NOW)
    assert result.executed and result.success
    assert runner.calls == [["docker", "restart", "worker"]]


def test_runner_failure_is_unsuccessful():
    ex = _executor(FakeRunner(rc=1, err="no such container"), dry_run=False)
    result = ex.execute(resolve("restart_container", {"service": "worker"}), now=NOW)
    assert result.executed and not result.success
    assert "no such container" in result.detail


def test_clear_queue_item_targets_redis():
    runner = FakeRunner()
    ex = _executor(runner, dry_run=False)
    ex.execute(resolve("clear_stuck_queue_item", {"item_id": "job-9"}), now=NOW)
    assert runner.calls[0][:3] == ["docker", "exec", "redis"]
    assert "job-9" in runner.calls[0]


def test_unsupported_action_is_inert():
    runner = FakeRunner()
    ex = _executor(runner, dry_run=False)
    result = ex.execute(resolve("flush_queue", {}), now=NOW)   # no docker mapping
    assert not result.executed and not result.success
    assert "does not support" in result.detail
    assert runner.calls == []


# --- guardrails (non-rate gates only; the restart cap lives in test_ratelimit.py) --------
def test_guardrails_allow_auto_action():
    decision = Guardrails().check(resolve("restart_container", {"service": "worker"}), now=NOW)
    assert decision.allowed


def test_guardrails_block_non_auto_action():
    decision = Guardrails().check(resolve("restart_container", {"service": "redis"}), now=NOW)
    assert not decision.allowed and "auto" in decision.reason.lower()   # redis is APPROVAL tier


def test_guardrails_block_invalid_action():
    decision = Guardrails().check(resolve("delete_database", {}), now=NOW)
    assert not decision.allowed
