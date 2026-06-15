"""TDD: the Kubernetes ActionBackend.

The production-shaped, free, local backend (runs against a `kind` cluster — see
deploy/k8s/). It mirrors the telemetry-adapter discipline (D-015): the HTTP transport is
injected, so request shaping and response parsing are unit-tested offline against canned API
JSON, and `observe()` degrades to None on any transport/parse failure rather than fabricating
a state. `apply()` is the *declarative, idempotent* equivalent of `kubectl rollout restart`
(a strategic-merge PATCH bumping the restartedAt annotation), and recovery is confirmed
against the Deployment's readyReplicas/observedGeneration — not a command exit code."""
import json
from datetime import datetime, timezone

from sre_agent.action.backend import ActionBackend, TargetState
from sre_agent.action.catalog import resolve
from sre_agent.action.k8s_backend import KubernetesActionBackend

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeKube:
    """Injected k8s transport: records (method, path, body), returns a canned (status, text).
    `routes` maps a (method) to (status, text); a single tuple applies to all calls."""

    def __init__(self, status=200, text="{}"):
        self.status, self.text = status, text
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        return self.status, self.text


def _backend(transport):
    return KubernetesActionBackend(transport=transport, namespace="lab",
                                   clock=lambda: NOW)


def test_is_an_action_backend():
    assert isinstance(_backend(FakeKube()), ActionBackend)


def test_supports_only_restart():
    b = _backend(FakeKube())
    assert b.supports("restart_container")
    assert not b.supports("clear_stuck_queue_item")   # redis-specific → inert on k8s
    assert not b.supports("flush_queue")


def test_apply_patches_deployment_with_restarted_at():
    kube = FakeKube(status=200, text="{}")
    b = _backend(kube)
    result = b.apply(resolve("restart_container", {"service": "api"}))
    assert result.executed and result.success
    method, path, body = kube.calls[0]
    assert method == "PATCH"
    assert path == "/apis/apps/v1/namespaces/lab/deployments/api"
    patch = json.loads(body)
    assert (patch["spec"]["template"]["metadata"]["annotations"]
            ["kubectl.kubernetes.io/restartedAt"] == NOW.isoformat())


def test_apply_unsupported_action_is_inert():
    kube = FakeKube()
    result = _backend(kube).apply(resolve("clear_stuck_queue_item", {"item_id": "x"}))
    assert not result.success and kube.calls == []


def test_apply_http_error_is_unsuccessful():
    kube = FakeKube(status=403, text='{"message":"forbidden"}')
    result = _backend(kube).apply(resolve("restart_container", {"service": "api"}))
    assert result.executed and not result.success
    assert "403" in result.detail


def test_observe_ready_deployment_is_healthy():
    body = json.dumps({"metadata": {"generation": 4},
                       "spec": {"replicas": 2},
                       "status": {"readyReplicas": 2, "observedGeneration": 4}})
    state = _backend(FakeKube(text=body)).observe(resolve("restart_container", {"service": "api"}))
    assert isinstance(state, TargetState) and state.exists and state.healthy


def test_observe_rolling_deployment_is_not_healthy():
    body = json.dumps({"metadata": {"generation": 5},
                       "spec": {"replicas": 2},
                       "status": {"readyReplicas": 1, "observedGeneration": 5}})
    state = _backend(FakeKube(text=body)).observe(resolve("restart_container", {"service": "api"}))
    assert state.exists and not state.healthy   # only 1 of 2 ready → still rolling


def test_observe_stale_generation_is_not_healthy():
    body = json.dumps({"metadata": {"generation": 6},
                       "spec": {"replicas": 1},
                       "status": {"readyReplicas": 1, "observedGeneration": 5}})
    state = _backend(FakeKube(text=body)).observe(resolve("restart_container", {"service": "api"}))
    assert state.exists and not state.healthy   # controller hasn't observed the new spec yet


def test_observe_missing_deployment():
    state = _backend(FakeKube(status=404, text='{"message":"not found"}')).observe(
        resolve("restart_container", {"service": "ghost"}))
    assert state is not None and not state.exists and not state.healthy


def test_observe_garbage_degrades_to_none():
    state = _backend(FakeKube(status=200, text="not json")).observe(
        resolve("restart_container", {"service": "api"}))
    assert state is None
