"""TDD: the action executor + guardrails. Tier-1 AUTO actions run against the lab via an
injected runner (so it's testable offline). Dry-run logs intent without executing. Guardrails
are code-enforced: only AUTO actions auto-execute, restarts are capped per service per hour,
and postgres data is never touched."""
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.action.catalog import resolve
from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.changelog import ChangeLog, ChangeLogEntry

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeRunner:
    def __init__(self, rc=0, out="ok", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return self.rc, self.out, self.err


# --- executor --------------------------------------------------------------------
def test_dry_run_does_not_execute():
    runner = FakeRunner()
    ex = ActionExecutor(runner=runner, dry_run=True)
    result = ex.execute(resolve("restart_container", {"service": "worker"}), now=NOW)
    assert result.dry_run and not result.executed and result.success
    assert "worker" in result.detail
    assert runner.calls == []          # nothing actually ran


def test_live_restart_runs_docker_restart():
    runner = FakeRunner()
    ex = ActionExecutor(runner=runner, dry_run=False)
    result = ex.execute(resolve("restart_container", {"service": "worker"}), now=NOW)
    assert result.executed and result.success
    assert runner.calls == [["docker", "restart", "worker"]]


def test_runner_failure_is_unsuccessful():
    ex = ActionExecutor(runner=FakeRunner(rc=1, err="no such container"), dry_run=False)
    result = ex.execute(resolve("restart_container", {"service": "worker"}), now=NOW)
    assert result.executed and not result.success
    assert "no such container" in result.detail


def test_clear_queue_item_targets_redis():
    runner = FakeRunner()
    ex = ActionExecutor(runner=runner, dry_run=False)
    ex.execute(resolve("clear_stuck_queue_item", {"item_id": "job-9"}), now=NOW)
    assert runner.calls[0][:3] == ["docker", "exec", "redis"]
    assert "job-9" in runner.calls[0]


# --- guardrails ------------------------------------------------------------------
@pytest.fixture
def changelog(tmp_path):
    return ChangeLog(tmp_path / "c.db")


def test_guardrails_allow_auto_action(changelog):
    g = Guardrails(max_restarts_per_hour=3, changelog=changelog)
    decision = g.check(resolve("restart_container", {"service": "worker"}), now=NOW)
    assert decision.allowed


def test_guardrails_block_non_auto_action(changelog):
    g = Guardrails(max_restarts_per_hour=3, changelog=changelog)
    decision = g.check(resolve("restart_container", {"service": "redis"}), now=NOW)  # APPROVAL tier
    assert not decision.allowed and "auto" in decision.reason.lower()


def test_guardrails_block_invalid_action(changelog):
    g = Guardrails(max_restarts_per_hour=3, changelog=changelog)
    decision = g.check(resolve("delete_database", {}), now=NOW)
    assert not decision.allowed


def test_restart_cap_blocks_after_limit(changelog):
    for i in range(3):
        changelog.record(ChangeLogEntry(ts=NOW - timedelta(minutes=10 + i), actor="sre-agent",
                                        service="worker", change_type="restart_container",
                                        detail="restart"))
    g = Guardrails(max_restarts_per_hour=3, changelog=changelog)
    decision = g.check(resolve("restart_container", {"service": "worker"}), now=NOW)
    assert not decision.allowed and "cap" in decision.reason.lower()


def test_restart_cap_is_per_service(changelog):
    for i in range(3):
        changelog.record(ChangeLogEntry(ts=NOW - timedelta(minutes=5), actor="sre-agent",
                                        service="worker", change_type="restart_container", detail="x"))
    g = Guardrails(max_restarts_per_hour=3, changelog=changelog)
    # api is a different service — not capped by worker's restarts
    assert g.check(resolve("restart_container", {"service": "api"}), now=NOW).allowed


def test_old_restarts_outside_hour_dont_count(changelog):
    for i in range(5):
        changelog.record(ChangeLogEntry(ts=NOW - timedelta(hours=2), actor="sre-agent",
                                        service="worker", change_type="restart_container", detail="x"))
    g = Guardrails(max_restarts_per_hour=3, changelog=changelog)
    assert g.check(resolve("restart_container", {"service": "worker"}), now=NOW).allowed
