"""Postgres-backed PostMortemStore (Maturity 9 / P1.2) — the HA twin of SqlitePostMortemStore.

Same `PostMortemStore` interface (the incident-memory read path and CompositePostMortemStore
are unchanged). Optionally still renders the markdown artifact to disk for the human record;
the queryable source of truth moves to the shared store so every replica reads the same memory.
The row→PostMortem mapper is reused verbatim from the SQLite store."""
from __future__ import annotations

import json
from pathlib import Path

from sre_agent.integrations.postmortems import (PostMortem, PostMortemStore, render_markdown,
                                                SqlitePostMortemStore as _SqlitePostMortemStore)
from sre_agent.pgdb import connect_pg

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postmortems (
    incident_id TEXT PRIMARY KEY,
    ticket_id TEXT,
    fingerprint TEXT NOT NULL,
    root_service TEXT NOT NULL,
    services TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    outcome TEXT NOT NULL,
    time_to_resolve_s DOUBLE PRECISION,
    created_at TEXT NOT NULL,
    timeline TEXT NOT NULL
)
"""


class PostgresPostMortemStore(PostMortemStore):
    def __init__(self, dsn: str, markdown_dir: str | Path | None = None) -> None:
        self._conn = connect_pg(dsn)
        self._conn.execute(_SCHEMA)
        self._md_dir = Path(markdown_dir) if markdown_dir else None

    def record(self, pm: PostMortem) -> None:
        self._conn.execute(
            "INSERT INTO postmortems (incident_id, ticket_id, fingerprint, root_service, "
            "services, root_cause, action_taken, outcome, time_to_resolve_s, created_at, "
            "timeline) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (incident_id) DO UPDATE SET "
            "ticket_id=EXCLUDED.ticket_id, fingerprint=EXCLUDED.fingerprint, "
            "root_service=EXCLUDED.root_service, services=EXCLUDED.services, "
            "root_cause=EXCLUDED.root_cause, action_taken=EXCLUDED.action_taken, "
            "outcome=EXCLUDED.outcome, time_to_resolve_s=EXCLUDED.time_to_resolve_s, "
            "created_at=EXCLUDED.created_at, timeline=EXCLUDED.timeline",
            (pm.incident_id, pm.ticket_id, pm.fingerprint, pm.root_service,
             json.dumps(pm.services), pm.root_cause, pm.action_taken, pm.outcome,
             pm.time_to_resolve_s, pm.created_at.isoformat(), json.dumps(pm.timeline)),
        )
        if self._md_dir is not None:
            self._md_dir.mkdir(parents=True, exist_ok=True)
            (self._md_dir / f"{pm.incident_id}.md").write_text(render_markdown(pm),
                                                               encoding="utf-8")

    def recent_for_fingerprint(self, fingerprint: str, limit: int = 3) -> list[PostMortem]:
        rows = self._conn.execute(
            "SELECT * FROM postmortems WHERE fingerprint = %s ORDER BY created_at DESC LIMIT %s",
            (fingerprint, limit)).fetchall()
        return [_SqlitePostMortemStore._from_row(r) for r in rows]

    def all(self) -> list[PostMortem]:
        rows = self._conn.execute(
            "SELECT * FROM postmortems ORDER BY created_at DESC").fetchall()
        return [_SqlitePostMortemStore._from_row(r) for r in rows]
