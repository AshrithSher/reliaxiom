"""Tiny demo driver: make the SRE agent's REAL KubernetesActionBackend restart a Deployment,
using the scoped ServiceAccount identity (not admin access). This is the exact call the agent
makes when it decides to remediate — here triggered by hand so you can watch it on the cluster.

Reads connection details from env (set by the demo runbook):
  SRE_KUBE_API_SERVER, SRE_KUBE_TOKEN, SRE_KUBE_CA   and optional SRE_KUBE_DEPLOYMENT (default 'worker')
"""
import os

from sre_agent.action.catalog import resolve
from sre_agent.action.k8s_backend import KubernetesActionBackend, build_kube_transport

service = os.getenv("SRE_KUBE_DEPLOYMENT", "worker")
transport = build_kube_transport(
    os.environ["SRE_KUBE_API_SERVER"], os.environ["SRE_KUBE_TOKEN"],
    ca_cert_path=os.environ["SRE_KUBE_CA"])
backend = KubernetesActionBackend(transport=transport, namespace="lab")
action = resolve("restart_container", {"service": service})

print(f"1. BEFORE — agent checks the live state of '{service}':")
print("   ", backend.observe(action))
print(f"\n2. AGENT ACTION — restart '{service}' (idempotent rollout via the k8s API):")
print("   ", backend.apply(action))
print(f"\n3. AFTER — agent re-checks the state:")
print("   ", backend.observe(action))
