"""TDD: the ActionBackend SPI and its lab default, DockerActionBackend.

The backend is the swappable execution seam (ROADMAP P0.1). It maps an abstract catalog
action to a substrate primitive; the engine never knows the substrate. DockerActionBackend
preserves the original executor's docker-argv behavior exactly, with the runner injected so
it's tested offline. `observe()` is the live-state read used for idempotency/verification."""
from datetime import datetime, timezone

from sre_agent.action.backend import (ActionBackend, BackendResult,
                                      DockerActionBackend, TargetState)
from sre_agent.action.catalog import resolve

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeRunner:
    """Injected docker runner: records argv, returns a canned (rc, out, err)."""

    def __init__(self, rc=0, out="ok", err=""):
        self.rc, self.out, self.err = rc, out, err
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return self.rc, self.out, self.err


def test_docker_backend_is_an_action_backend():
    assert isinstance(DockerActionBackend(), ActionBackend)


def test_supports_only_docker_mapped_actions():
    b = DockerActionBackend()
    assert b.supports("restart_container")
    assert b.supports("clear_stuck_queue_item")
    assert b.supports("rerun_failed_job")
    assert not b.supports("flush_queue")       # no docker mapping → inert
    assert not b.supports("change_config")


def test_apply_restart_runs_docker_restart():
    runner = FakeRunner()
    b = DockerActionBackend(runner=runner)
    result = b.apply(resolve("restart_container", {"service": "worker"}))
    assert isinstance(result, BackendResult)
    assert result.executed and result.success
    assert runner.calls == [["docker", "restart", "worker"]]


def test_apply_clear_queue_item_targets_redis():
    runner = FakeRunner()
    b = DockerActionBackend(runner=runner)
    b.apply(resolve("clear_stuck_queue_item", {"item_id": "job-9"}))
    assert runner.calls[0][:3] == ["docker", "exec", "redis"]
    assert "job-9" in runner.calls[0]


def test_apply_failure_is_unsuccessful():
    b = DockerActionBackend(runner=FakeRunner(rc=1, err="no such container"))
    result = b.apply(resolve("restart_container", {"service": "worker"}))
    assert result.executed and not result.success
    assert "no such container" in result.detail


def test_observe_running_container_is_healthy():
    runner = FakeRunner(out='[{"Name": "/worker", "State": {"Status": "running"}}]')
    state = DockerActionBackend(runner=runner).observe(
        resolve("restart_container", {"service": "worker"}))
    assert isinstance(state, TargetState)
    assert state.exists and state.healthy and state.detail == "running"
    assert runner.calls == [["docker", "inspect", "worker"]]


def test_observe_missing_container_does_not_exist():
    state = DockerActionBackend(runner=FakeRunner(rc=1, err="No such object")).observe(
        resolve("restart_container", {"service": "ghost"}))
    assert state is not None and not state.exists and not state.healthy


def test_observe_garbage_degrades_to_none():
    state = DockerActionBackend(runner=FakeRunner(out="not json")).observe(
        resolve("restart_container", {"service": "worker"}))
    assert state is None    # unparseable → no reading, never a fabricated one (D-015)


def test_observe_without_service_target_is_none():
    state = DockerActionBackend(runner=FakeRunner()).observe(
        resolve("clear_stuck_queue_item", {"item_id": "job-9"}))
    assert state is None
