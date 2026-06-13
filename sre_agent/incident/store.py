"""SQLite persistence for incidents. Source of truth for incident state; the manager uses
it to dedup (open incident per fingerprint) and to detect flapping (latest resolved)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident


class IncidentStore:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
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
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (CREATE TABLE IF NOT EXISTS
        won't alter an existing table). Keeps a pre-M4 incidents.db loadable."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(incidents)")}
        for name, ddl in (("remediation_loops", "INTEGER NOT NULL DEFAULT 0"),
                          ("verify_started_at", "TEXT"),
                          ("pending_action", "TEXT"),
                          ("pending_params", "TEXT"),
                          ("approval_deadline", "TEXT"),
                          ("diagnosis_attempts", "INTEGER NOT NULL DEFAULT 0"),
                          ("root_cause", "TEXT NOT NULL DEFAULT ''"),
                          ("action_taken", "TEXT NOT NULL DEFAULT ''")):
            if name not in existing:
                self._conn.execute(f"ALTER TABLE incidents ADD COLUMN {name} {ddl}")

    def save(self, incident: Incident) -> None:
        self._conn.execute(
            "INSERT INTO incidents (id, fingerprint, ticket_id, state, root_service, "
            "services, fault_type, severity, first_seen, updated_at, resolved_at, flap_count, "
            "remediation_loops, verify_started_at, pending_action, pending_params, "
            "approval_deadline, diagnosis_attempts, root_cause, action_taken) "
            "VALUES (:id,:fingerprint,:ticket_id,:state,:root_service,:services,:fault_type,"
            ":severity,:first_seen,:updated_at,:resolved_at,:flap_count,"
            ":remediation_loops,:verify_started_at,:pending_action,:pending_params,"
            ":approval_deadline,:diagnosis_attempts,:root_cause,:action_taken) "
            "ON CONFLICT(id) DO UPDATE SET "
            "fingerprint=excluded.fingerprint, ticket_id=excluded.ticket_id, "
            "state=excluded.state, root_service=excluded.root_service, "
            "services=excluded.services, fault_type=excluded.fault_type, "
            "severity=excluded.severity, updated_at=excluded.updated_at, "
            "resolved_at=excluded.resolved_at, flap_count=excluded.flap_count, "
            "remediation_loops=excluded.remediation_loops, "
            "verify_started_at=excluded.verify_started_at, "
            "pending_action=excluded.pending_action, pending_params=excluded.pending_params, "
            "approval_deadline=excluded.approval_deadline, "
            "diagnosis_attempts=excluded.diagnosis_attempts, "
            "root_cause=excluded.root_cause, action_taken=excluded.action_taken",
            self._to_row(incident),
        )
        self._conn.commit()

    def get(self, incident_id: str) -> Incident | None:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return self._from_row(row) if row else None

    def find_open_by_fingerprint(self, fingerprint: str) -> Incident | None:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE fingerprint = ? AND state != ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (fingerprint, IncidentState.RESOLVED.value),
        ).fetchone()
        return self._from_row(row) if row else None

    def find_by_state(self, state: IncidentState) -> list[Incident]:
        rows = self._conn.execute(
            "SELECT * FROM incidents WHERE state = ? ORDER BY updated_at",
            (state.value,)).fetchall()
        return [self._from_row(r) for r in rows]

    def find_active(self) -> list[Incident]:
        """Incidents still in flight — neither resolved nor handed off (escalated)."""
        rows = self._conn.execute(
            "SELECT * FROM incidents WHERE state NOT IN (?, ?) ORDER BY updated_at",
            (IncidentState.RESOLVED.value, IncidentState.ESCALATED.value)).fetchall()
        return [self._from_row(r) for r in rows]

    def find_latest_resolved(self, fingerprint: str) -> Incident | None:
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE fingerprint = ? AND state = ? "
            "ORDER BY resolved_at DESC LIMIT 1",
            (fingerprint, IncidentState.RESOLVED.value),
        ).fetchone()
        return self._from_row(row) if row else None

    @staticmethod
    def _to_row(inc: Incident) -> dict:
        return {
            "id": inc.id, "fingerprint": inc.fingerprint, "ticket_id": inc.ticket_id,
            "state": inc.state.value, "root_service": inc.root_service,
            "services": json.dumps(inc.services), "fault_type": inc.fault_type,
            "severity": inc.severity, "first_seen": inc.first_seen.isoformat(),
            "updated_at": inc.updated_at.isoformat(),
            "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
            "flap_count": inc.flap_count,
            "remediation_loops": inc.remediation_loops,
            "verify_started_at": inc.verify_started_at.isoformat() if inc.verify_started_at else None,
            "pending_action": inc.pending_action,
            "pending_params": json.dumps(inc.pending_params),
            "approval_deadline": inc.approval_deadline.isoformat() if inc.approval_deadline else None,
            "diagnosis_attempts": inc.diagnosis_attempts,
            "root_cause": inc.root_cause,
            "action_taken": inc.action_taken,
        }

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Incident:
        return Incident(
            id=row["id"], fingerprint=row["fingerprint"], ticket_id=row["ticket_id"],
            state=IncidentState(row["state"]), root_service=row["root_service"],
            services=json.loads(row["services"]), fault_type=row["fault_type"],
            severity=row["severity"],
            first_seen=datetime.fromisoformat(row["first_seen"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            flap_count=row["flap_count"],
            remediation_loops=row["remediation_loops"],
            verify_started_at=(datetime.fromisoformat(row["verify_started_at"])
                               if row["verify_started_at"] else None),
            pending_action=row["pending_action"],
            pending_params=json.loads(row["pending_params"]) if row["pending_params"] else {},
            approval_deadline=(datetime.fromisoformat(row["approval_deadline"])
                               if row["approval_deadline"] else None),
            diagnosis_attempts=row["diagnosis_attempts"],
            root_cause=row["root_cause"],
            action_taken=row["action_taken"],
        )
