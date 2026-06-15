"""Kubernetes ActionBackend — the production-shaped execution surface (ROADMAP P0.1).

This replaces "shell `docker restart` on the local socket (≈ root on the host)" with a call
to the Kubernetes API under a **scoped, least-privilege** identity: the agent's ServiceAccount
is granted exactly `get/list/patch` on Deployments in one namespace (see deploy/k8s/rbac.yaml),
so its blast radius is an explicit RBAC grant, not the whole host. It runs unchanged against a
free local `kind` cluster — architecturally identical to a managed cluster, no cloud required.

Two production properties the docker backend lacked:
  - **Declarative idempotency.** A restart is a strategic-merge PATCH that bumps the
    `restartedAt` annotation (the `kubectl rollout restart` mechanism). Re-applying converges to
    the same desired state instead of imperatively killing a container.
  - **Verification against reality.** `observe()` reads the Deployment's readyReplicas and
    observedGeneration, so "did the restart succeed" is answered by the cluster, not by a CLI
    exit code that only means "the request was accepted".

Transport is injected and every read degrades to None on failure (D-015); the exact API wire
shapes are validated at integration time against a real cluster (like the Tempo adapter)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from sre_agent.action.backend import BackendResult, TargetState
from sre_agent.action.catalog import ResolvedAction

# transport(method, path, body) -> (status_code, response_text). Injected for testability.
KubeTransport = Callable[[str, str, str | None], tuple[int, str]]


class KubernetesActionBackend:
    def __init__(self, transport: KubeTransport, namespace: str = "lab",
                 clock: Callable[[], datetime] | None = None,
                 deployment_of: Callable[[str], str] | None = None) -> None:
        self._t = transport
        self._ns = namespace
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # service name -> Deployment name. Identity by default; a real cluster may differ.
        self._deployment_of = deployment_of or (lambda service: service)

    def supports(self, action_id: str) -> bool:
        # Only a Deployment rollout-restart maps cleanly to the k8s API here; the redis queue
        # ops are substrate-specific and stay inert until a backend models them (invariant #2).
        return action_id == "restart_container"

    def _deploy_path(self, service: str) -> str:
        return f"/apis/apps/v1/namespaces/{self._ns}/deployments/{self._deployment_of(service)}"

    def apply(self, action: ResolvedAction) -> BackendResult:
        if not self.supports(action.action_id):
            return BackendResult(executed=False, success=False,
                                 detail=f"k8s backend does not support {action.action_id}")
        service = action.params.get("service")
        if not service:
            return BackendResult(executed=False, success=False,
                                 detail="restart_container requires a 'service'")
        # strategic-merge PATCH: bump restartedAt → controller rolls the pods (idempotent)
        body = json.dumps({"spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": self._clock().isoformat()}}}}})
        try:
            status, text = self._t("PATCH", self._deploy_path(service), body)
        except Exception as exc:  # noqa: BLE001 — surface as a failed action, never crash the loop
            return BackendResult(executed=True, success=False, detail=f"transport error: {exc}")
        if 200 <= status < 300:
            return BackendResult(executed=True, success=True,
                                 detail=f"rollout restart issued (HTTP {status})")
        return BackendResult(executed=True, success=False, detail=f"HTTP {status}: {text[:200]}")

    def observe(self, action: ResolvedAction) -> TargetState | None:
        service = action.params.get("service")
        if not service:
            return None
        try:
            status, text = self._t("GET", self._deploy_path(service), None)
        except Exception:  # noqa: BLE001 — unreachable → no reading (degrade, never fabricate)
            return None
        if status == 404:
            return TargetState(exists=False, healthy=False, detail="deployment not found")
        if not (200 <= status < 300):
            return None
        try:
            d = json.loads(text)
            desired = int(d.get("spec", {}).get("replicas", 1))
            st = d.get("status", {})
            ready = int(st.get("readyReplicas", 0))
            generation = int(d.get("metadata", {}).get("generation", 0))
            observed = int(st.get("observedGeneration", 0))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            return None
        # healthy = the controller has observed the latest spec AND all replicas are ready
        healthy = observed >= generation and ready >= desired
        return TargetState(exists=True, healthy=healthy,
                           detail=f"ready {ready}/{desired}, gen {observed}/{generation}")


def build_kube_transport(api_server: str, token: str, ca_cert_path: str | None = None,
                         timeout_s: float = 10.0) -> KubeTransport:
    """Real transport against the k8s API: bearer-token auth (the mounted ServiceAccount token
    in-cluster, or a kind kubeconfig token locally) + strategic-merge content type. Wire
    details are integration-validated; unit tests inject a fake transport instead."""
    import ssl
    import urllib.error
    import urllib.request

    ctx = ssl.create_default_context(cafile=ca_cert_path) if ca_cert_path else None

    def transport(method: str, path: str, body: str | None) -> tuple[int, str]:
        req = urllib.request.Request(
            api_server.rstrip("/") + path, method=method,
            data=body.encode("utf-8") if body else None,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json",
                     "Content-Type": "application/strategic-merge-patch+json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    return transport
