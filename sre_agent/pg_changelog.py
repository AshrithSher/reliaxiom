"""Postgres-backed ChangeLog (Maturity 9 / P1.2) — the HA twin of the SQLite ChangeLog.

Same `record`/`recent` surface the diagnosis context and detection-suppression already use.
Crucially it shares the `changes` table with `PostgresRateLimiter` in the same database, so the
restart-cap count and the invariant-#5 agent-action tags are one fleet-wide ledger — a restart
recorded by any replica is counted by every replica's cap check and ignored by every replica's
detector (no agent diagnosing another agent's remediation)."""
from __future__ import annotations

from datetime import datetime

from sre_agent.changelog import ChangeLogEntry
from sre_agent.pgdb import connect_pg

_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id BIGSERIAL PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    service TEXT NOT NULL,
    change_type TEXT NOT NULL,
    detail TEXT NOT NULL
)
"""


class PostgresChangeLog:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn = connect_pg(dsn)
        self._conn.execute(_SCHEMA)

    def record(self, entry: ChangeLogEntry) -> None:
        self._conn.execute(
            "INSERT INTO changes (ts, actor, service, change_type, detail) VALUES (%s,%s,%s,%s,%s)",
            (entry.ts.isoformat(), entry.actor, entry.service, entry.change_type, entry.detail))

    def recent(self, since: datetime, until: datetime) -> list[ChangeLogEntry]:
        rows = self._conn.execute(
            "SELECT * FROM changes WHERE ts >= %s AND ts <= %s ORDER BY ts",
            (since.isoformat(), until.isoformat())).fetchall()
        return [ChangeLogEntry(ts=datetime.fromisoformat(r["ts"]), actor=r["actor"],
                               service=r["service"], change_type=r["change_type"],
                               detail=r["detail"]) for r in rows]
