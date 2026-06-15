"""Postgres-backed TicketStore (Maturity 9 / P1.2) — the HA twin of SqliteTicketStore.

Implements the same `TicketStore` interface, so the manager and the CompositeTicketStore
(SQLite/Jira side-by-side) are unchanged: this simply swaps the local authoritative store from
a per-host file to the shared managed store. Sequential keys come from a Postgres SEQUENCE
(`nextval` is atomic across replicas), and the row→Ticket mapper is reused verbatim from the
SQLite store so the two backends can't drift."""
from __future__ import annotations

import json
from datetime import datetime

from sre_agent.integrations.ticketing import (Comment, Ticket, TicketStatus, TicketStore,
                                              SqliteTicketStore as _SqliteTicketStore)
from sre_agent.pgdb import connect_pg

_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS ticket_seq;
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    seq BIGINT UNIQUE,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    services TEXT NOT NULL,
    fault_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    assignee TEXT,
    evidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id BIGSERIAL PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL
);
"""


class PostgresTicketStore(TicketStore):
    def __init__(self, dsn: str) -> None:
        self._conn = connect_pg(dsn)
        for stmt in filter(str.strip, _SCHEMA.split(";")):
            self._conn.execute(stmt)

    def create(self, *, fingerprint: str, title: str, services: list[str], fault_type: str,
               severity: str, evidence: str, now: datetime,
               ticket_id: str | None = None) -> Ticket:
        seq = int(self._conn.execute("SELECT nextval('ticket_seq') AS n").fetchone()["n"])
        ticket = Ticket(
            id=ticket_id or f"SRE-{seq}", fingerprint=fingerprint, title=title, services=services,
            fault_type=fault_type, severity=severity, status=TicketStatus.OPEN,
            evidence=evidence, created_at=now, updated_at=now,
        )
        self._conn.execute(
            "INSERT INTO tickets (id, seq, fingerprint, title, services, fault_type, severity, "
            "status, assignee, evidence, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ticket.id, seq, fingerprint, title, json.dumps(services), fault_type, severity,
             ticket.status.value, None, evidence, now.isoformat(), now.isoformat()),
        )
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        row = self._conn.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,)).fetchone()
        if row is None:
            return None
        comments = [
            Comment(ts=datetime.fromisoformat(c["ts"]), author=c["author"], body=c["body"])
            for c in self._conn.execute(
                "SELECT * FROM comments WHERE ticket_id = %s ORDER BY id", (ticket_id,)).fetchall()
        ]
        return _SqliteTicketStore._row_to_ticket(row, comments)

    def find_open_by_fingerprint(self, fingerprint: str) -> Ticket | None:
        row = self._conn.execute(
            "SELECT id FROM tickets WHERE fingerprint = %s AND status != %s "
            "ORDER BY seq DESC LIMIT 1",
            (fingerprint, TicketStatus.RESOLVED.value)).fetchone()
        return self.get(row["id"]) if row else None

    def add_comment(self, ticket_id: str, *, author: str, body: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO comments (ticket_id, ts, author, body) VALUES (%s,%s,%s,%s)",
            (ticket_id, now.isoformat(), author, body))
        self._touch(ticket_id, now)

    def set_status(self, ticket_id: str, status: TicketStatus, *, now: datetime) -> None:
        self._conn.execute("UPDATE tickets SET status = %s WHERE id = %s",
                           (status.value, ticket_id))
        self._touch(ticket_id, now)

    def assign(self, ticket_id: str, assignee: str, *, now: datetime) -> None:
        self._conn.execute("UPDATE tickets SET assignee = %s WHERE id = %s",
                           (assignee, ticket_id))
        self._touch(ticket_id, now)

    def list_open(self) -> list[Ticket]:
        rows = self._conn.execute(
            "SELECT id FROM tickets WHERE status != %s ORDER BY seq",
            (TicketStatus.RESOLVED.value,)).fetchall()
        return [t for t in (self.get(r["id"]) for r in rows) if t is not None]

    def _touch(self, ticket_id: str, now: datetime) -> None:
        self._conn.execute("UPDATE tickets SET updated_at = %s WHERE id = %s",
                           (now.isoformat(), ticket_id))
