"""Backend selection for the agent's control-plane state (Maturity 9 / P1.2).

One place decides whether incidents, the change log, tickets, post-mortems, the restart-cap
ledger, and leader election live in per-host SQLite (default, restart-safe) or a shared Postgres
store (HA, failure-safe). Everything downstream keeps consuming the same interfaces — the manager,
the Composite Jira/Confluence stores, the rate limiter, and the diagnosis read paths are all
unchanged. This is the "swap the backing store + add coordination" the persistence-interface
discipline (D-006/D-009) was built for, not a redesign."""
from __future__ import annotations

from pathlib import Path

from sre_agent.changelog import ChangeLog
from sre_agent.config import Config
from sre_agent.ha.leader import Leadership, PostgresLeadership, SingleNodeLeadership
from sre_agent.incident.store import IncidentStore
from sre_agent.integrations.postmortems import PostMortemStore, SqlitePostMortemStore
from sre_agent.integrations.ticketing import SqliteTicketStore, TicketStore


def _is_pg(cfg: Config) -> bool:
    return getattr(cfg, "state_backend", "sqlite") == "postgres"


def build_changelog(cfg: Config, data_dir: Path):
    if _is_pg(cfg):
        from sre_agent.pg_changelog import PostgresChangeLog
        from sre_agent.pgdb import resolve_dsn
        return PostgresChangeLog(resolve_dsn(cfg))
    return ChangeLog(data_dir / "changes.db")


def build_incident_store(cfg: Config, data_dir: Path):
    if _is_pg(cfg):
        from sre_agent.incident.pg_store import PostgresIncidentStore
        from sre_agent.pgdb import resolve_dsn
        return PostgresIncidentStore(resolve_dsn(cfg))
    return IncidentStore(data_dir / "incidents.db")


def build_local_ticket_store(cfg: Config, data_dir: Path) -> TicketStore:
    """The authoritative local ticket store (the Composite Jira store wraps this one)."""
    if _is_pg(cfg):
        from sre_agent.integrations.pg_ticketing import PostgresTicketStore
        from sre_agent.pgdb import resolve_dsn
        return PostgresTicketStore(resolve_dsn(cfg))
    return SqliteTicketStore(data_dir / "tickets.db")


def build_local_postmortem_store(cfg: Config, data_dir: Path) -> PostMortemStore:
    if _is_pg(cfg):
        from sre_agent.integrations.pg_postmortems import PostgresPostMortemStore
        from sre_agent.pgdb import resolve_dsn
        return PostgresPostMortemStore(resolve_dsn(cfg), markdown_dir=data_dir / "postmortems")
    return SqlitePostMortemStore(data_dir / "postmortems.db",
                                 markdown_dir=data_dir / "postmortems")


def build_leadership(cfg: Config) -> Leadership:
    """PostgresLeadership when HA is on (requires the postgres backend); else the single-node
    no-op leader. Refusing HA without a shared store is deliberate — leader election over
    per-host SQLite would be meaningless (each replica would 'lead' its own file)."""
    if getattr(cfg, "ha_enabled", False):
        if not _is_pg(cfg):
            raise RuntimeError("ha_enabled requires state_backend=postgres "
                               "(leader election needs the shared store)")
        from sre_agent.pgdb import resolve_dsn
        return PostgresLeadership(resolve_dsn(cfg), lock_key=cfg.leader_lock_key,
                                  replica_id=cfg.replica_id)
    return SingleNodeLeadership()
