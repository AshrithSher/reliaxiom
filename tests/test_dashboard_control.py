"""TDD: the dashboard control plane's pure logic — the fault catalog and command shaping.
Subprocess execution and the agent runner are exercised live in the demo, not here."""
import sre_agent.dashboard.control as control


def test_every_fault_has_label_tier_blurb_and_inject():
    for name, f in control.DEMO_FAULTS.items():
        assert f["label"] and f["blurb"]
        assert f["tier"] in ("auto", "approval")
        assert f["inject"] and all(isinstance(cmd, list) for cmd in f["inject"])


def test_stateful_faults_are_approval_tier():
    # postgres/redis restarts must be human-approved (they are stateful)
    assert control.DEMO_FAULTS["db-down"]["tier"] == "approval"
    assert control.DEMO_FAULTS["redis-down"]["tier"] == "approval"
    assert control.DEMO_FAULTS["auth-down"]["tier"] == "auto"


def test_chaos_faults_target_the_right_service():
    # auth/payments fail via an in-container chaos toggle (fail fast, no DNS hang)
    assert control.DEMO_FAULTS["auth-down"]["inject"][0][:3] == ["docker", "exec", "auth"]
    assert control.DEMO_FAULTS["payments-down"]["inject"][0][:3] == ["docker", "exec", "payments"]
    # worker/postgres/redis fail via docker stop (no HTTP call in their failure path)
    assert control.DEMO_FAULTS["kill-worker"]["inject"] == [["docker", "stop", "worker"]]
    assert control.DEMO_FAULTS["db-down"]["inject"] == [["docker", "stop", "postgres"]]


def test_run_fault_unknown_name_is_an_error():
    out = control.run_fault("does-not-exist", "/tmp")
    assert out["ok"] is False and "unknown" in out["error"]


def test_run_fault_executes_each_inject_command(monkeypatch):
    calls = []

    class _OK:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(control.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _OK())
    out = control.run_fault("auth-down", "/lab")
    assert out["ok"] is True and out["errors"] == []
    assert calls == control.DEMO_FAULTS["auth-down"]["inject"]


def test_reset_clears_chaos_and_restarts_containers(monkeypatch):
    calls = []

    class _OK:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(control.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _OK())
    control.reset_lab("/lab")
    # turns the api error + latency toggles off and starts the stoppable containers
    assert ["docker", "start", "postgres"] in calls
    assert any(c[:3] == ["docker", "exec", "auth"] for c in calls)
