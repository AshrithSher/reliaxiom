"""Postgres-backed incident store (Maturity 9 / P1.2) — the HA twin of the SQLite
`IncidentStore`. Same methods, same Incident model, so the Incident Manager is unchanged;
only the backing store differs. Multiple replicas read/write one shared table, so a standby
resumes mid-incident after the leader dies (D-009 at fleet scale).

The row↔model mappers (`_to_row`/`_from_row`) are reused verbatim from the SQLite store —
they are pure dict/Row logic with no SQLite specifics — so the two backends can never drift
in how an Incident is serialised."""
from __future__ import annotations

from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore as _SqliteIncidentStore
from sre_agent.pgdb import connect_pg

_COLS = ("id, fingerprint, ticket_id, state, root_service, services, fault_type, severity, "
         "first_seen, updated_at, resolved_at, flap_count, remediation_loops, "
         "verify_started_at, pending_action, pending_params, approval_deadline, "
         "diagnosis_attempts, root_cause, action_taken")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    ticket_id TEXT,
    state TEXT NOT NULL,
    root_service TEXT NOT NULL,
    services TEXT NOT NULL,
    fault_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    flap_count INTEGER NOT NULL DEFAULT 0,
    remediation_loops INTEGER NOT NULL DEFAULT 0,
    verify_started_at TEXT,
    pending_action TEXT,
    pending_params TEXT,
    approval_deadline TEXT,
    diagnosis_attempts INTEGER NOT NULL DEFAULT 0,
    root_cause TEXT NOT NULL DEFAULT '',
    action_taken TEXT NOT NULL DEFAULT ''
)
"""


class PostgresIncidentStore:
    def __init__(self, dsn: str) -> None:
        self._conn = connect_pg(dsn)
        self._conn.execute(_SCHEMA)

    def save(self, incident: Incident) -> None:
        placeholders = ", ".join(f"%({c.strip()})s" for c in _COLS.split(","))
        updates = ", ".join(f"{c.strip()}=EXCLUDED.{c.strip()}"
                            for c in _COLS.split(",") if c.strip() != "id")
        self._conn.execute(
            f"INSERT INTO incidents ({_COLS}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            _SqliteIncidentStore._to_row(incident),
        )

    def get(self, incident_id: str) -> Incident | None:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE id = %s", (incident_id,)).fetchone()
        return _SqliteIncidentStore._from_row(row) if row else None

    def find_open_by_fingerprint(self, fingerprint: str) -> Incident | None:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE fingerprint = %s AND state != %s "
            "ORDER BY updated_at DESC LIMIT 1",
            (fingerprint, IncidentState.RESOLVED.value)).fetchone()
        return _SqliteIncidentStore._from_row(row) if row else None

    def find_by_state(self, state: IncidentState) -> list[Incident]:
        rows = self._conn.execute(
            "SELECT * FROM incidents WHERE state = %s ORDER BY updated_at",
            (state.value,)).fetchall()
        return [_SqliteIncidentStore._from_row(r) for r in rows]

    def find_active(self) -> list[Incident]:
        rows = self._conn.execute(
            "SELECT * FROM incidents WHERE state NOT IN (%s, %s) ORDER BY updated_at",
            (IncidentState.RESOLVED.value, IncidentState.ESCALATED.value)).fetchall()
        return [_SqliteIncidentStore._from_row(r) for r in rows]

    def find_latest_resolved(self, fingerprint: str) -> Incident | None:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE fingerprint = %s AND state = %s "
            "ORDER BY resolved_at DESC LIMIT 1",
            (fingerprint, IncidentState.RESOLVED.value)).fetchone()
        return _SqliteIncidentStore._from_row(row) if row else None
