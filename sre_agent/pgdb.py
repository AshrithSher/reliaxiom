"""Shared Postgres connection helper for the agent's control-plane state (Maturity 9 / P1.2).

This backs the *agent's own* stores — incidents, tickets, change log, post-mortems, the
restart-cap ledger, and the leader lock — when `state_backend=postgres`. It is the HA swap for
the per-host SQLite files (sre_agent/db.py): a managed shared store multiple replicas point at,
so the agent is failure-safe, not merely restart-safe (D-009).

This database is deliberately SEPARATE from any monitored system's data store. Invariant #4 —
the agent never performs a destructive op on the monitored system's data of record — is about
the *managed* system; this is the agent's control plane, its own to own. The dependency is
optional (`pip install 'sre-agent[postgres]'`); importing this module without psycopg installed
raises a clear, actionable error only when a Postgres backend is actually selected.
"""
from __future__ import annotations

import os

try:  # psycopg is an optional extra — only needed when state_backend=postgres
    import psycopg
    from psycopg.rows import dict_row
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 - surface a clean message at use time, not import time
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def require_psycopg() -> None:
    if psycopg is None:
        raise RuntimeError(
            "state_backend=postgres requires psycopg — install with "
            "`pip install 'sre-agent[postgres]'` (or `pip install psycopg[binary]`). "
            f"Original import error: {_IMPORT_ERROR}")


def resolve_dsn(cfg) -> str:
    """The Postgres DSN from the configured env var, falling back to cfg.state_dsn."""
    dsn = os.environ.get(cfg.state_dsn_env, "") or cfg.state_dsn
    if not dsn:
        raise RuntimeError(
            f"state_backend=postgres needs a DSN — set ${cfg.state_dsn_env} or state_dsn "
            "(e.g. 'host=localhost port=5433 dbname=sre user=sre password=...')")
    return dsn


def connect_pg(dsn: str):
    """One autocommit connection with dict rows — so row['col'] access mirrors sqlite3.Row and
    the existing row→model mappers are reused verbatim. Callers that need atomicity open an
    explicit transaction (`with conn.transaction(): ...`); everything else is single statements."""
    require_psycopg()
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
