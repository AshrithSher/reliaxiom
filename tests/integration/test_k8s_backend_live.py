"""Cluster-backed smoke test for the Kubernetes backend's real transport.

Skipped unless a kind cluster is wired via env (so the offline suite is unaffected). This is
where the actual k8s API wire shapes are validated — the unit tests in test_k8s_backend.py
cover the request shaping and parsing against canned JSON. Bring a cluster up with
scripts/setup-kind.ps1, then:

    $env:SRE_KIND_E2E = '1'
    $env:SRE_KUBE_API_SERVER = '<server>'; $env:SRE_KUBE_TOKEN = '<token>'
    $env:SRE_KUBE_DEPLOYMENT = 'api'        # an existing Deployment in the lab namespace
    pytest tests/integration/test_k8s_backend_live.py
"""
import os

import pytest

from sre_agent.action.backend import TargetState
from sre_agent.action.catalog import resolve
from sre_agent.action.k8s_backend import KubernetesActionBackend, build_kube_transport

pytestmark = pytest.mark.skipif(
    os.getenv("SRE_KIND_E2E") != "1",
    reason="set SRE_KIND_E2E=1 with a kind cluster wired via env to run")


def _backend():
    transport = build_kube_transport(
        os.environ["SRE_KUBE_API_SERVER"], os.environ["SRE_KUBE_TOKEN"],
        ca_cert_path=os.getenv("SRE_KUBE_CA"))
    return KubernetesActionBackend(transport=transport, namespace=os.getenv("SRE_KUBE_NS", "lab"))


def test_observe_real_deployment_returns_state():
    action = resolve("restart_container", {"service": os.environ["SRE_KUBE_DEPLOYMENT"]})
    state = _backend().observe(action)
    assert isinstance(state, TargetState) and state.exists


def test_restart_real_deployment_then_recovers():
    action = resolve("restart_container", {"service": os.environ["SRE_KUBE_DEPLOYMENT"]})
    b = _backend()
    result = b.apply(action)
    assert result.executed and result.success
    # the rollout takes time; observe() is the verification surface (poll in the real loop)
    assert b.observe(action) is not None
