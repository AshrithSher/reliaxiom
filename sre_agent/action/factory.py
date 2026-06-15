"""Construct the action backend and rate limiter from config — the twins of
`build_live_telemetry_sources` (telemetry/adapters.py). Selecting a substrate is config, not
code: the engine only ever sees the ActionBackend / ActionRateLimiter interfaces.

Defaults keep the lab on docker + atomic SQLite so the offline suite is untouched; flip
`action_backend` to "kubernetes" to drive a local kind cluster via the k8s API (no cloud)."""
from __future__ import annotations

import os

from sre_agent.action.backend import ActionBackend, DockerActionBackend
from sre_agent.action.k8s_backend import KubernetesActionBackend, build_kube_transport
from sre_agent.action.ratelimit import ActionRateLimiter, SqliteRateLimiter
from sre_agent.config import Config


def build_action_backend(cfg: Config) -> ActionBackend:
    if getattr(cfg, "action_backend", "docker") == "kubernetes":
        token = os.environ.get(cfg.kube_token_env, "")
        server = cfg.kube_api_server or os.environ.get("SRE_KUBE_API_SERVER", "")
        transport = build_kube_transport(server, token, ca_cert_path=cfg.kube_ca_cert or None)
        return KubernetesActionBackend(transport=transport, namespace=cfg.kube_namespace)
    return DockerActionBackend()


def build_rate_limiter(cfg: Config, changelog) -> ActionRateLimiter:
    # The fleet-wide cap must live in the same store as the change log it counts. When the
    # state backend is Postgres (HA), use the fleet-atomic PostgresRateLimiter over the shared
    # `changes` table (advisory-lock serialised across replicas); otherwise the per-host atomic
    # SqliteRateLimiter over the local change-log file. `guardrail_store` can force Postgres
    # independently for operators who shard only the cap onto a shared store.
    use_pg = (getattr(cfg, "state_backend", "sqlite") == "postgres"
              or getattr(cfg, "guardrail_store", "sqlite") == "postgres")
    if use_pg:
        from sre_agent.action.pg_ratelimit import PostgresRateLimiter
        from sre_agent.pgdb import resolve_dsn
        dsn = getattr(changelog, "dsn", None) or resolve_dsn(cfg)
        return PostgresRateLimiter(cfg.max_restarts_per_hour, dsn)
    return SqliteRateLimiter(cfg.max_restarts_per_hour, changelog.path)
