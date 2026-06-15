"""Ticketing integration boundary.

`TicketStore` is the interface the Incident Manager talks to; `SqliteTicketStore` is the
default local stub. The shape mirrors JIRA (sequential keys, a status lifecycle, comments,
an assignee) so the real JIRA adapter (M7) is a mechanical swap. The store persists and
answers dedup queries; it does NOT enforce one-ticket-per-fault — that policy lives in the
Incident Manager, which calls find_open_by_fingerprint before creating.
"""
from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from sre_agent.db import connect


class TicketStatus(str, Enum):
    OPEN = "Open"
    DIAGNOSING = "Diagnosing"
    AWAITING_APPROVAL = "AwaitingApproval"
    ACTING = "Acting"
    VERIFYING = "Verifying"
    RESOLVED = "Resolved"
    ESCALATED = "Escalated"

    @property
    def is_open(self) -> bool:
        """Open = not yet resolved. Escalated is still open (assigned to a human)."""
        return self is not TicketStatus.RESOLVED


class Comment(BaseModel):
    ts: datetime
    author: str
    body: str


class Ticket(BaseModel):
    id: str
    fingerprint: str
    title: str
    services: list[str]
    fault_type: str
    severity: str
    status: TicketStatus = TicketStatus.OPEN
    assignee: str | None = None
    evidence: str = ""
    created_at: datetime
    updated_at: datetime
    comments: list[Comment] = Field(default_factory=list)


class TicketStore(ABC):
    @abstractmethod
    def create(self, *, fingerprint: str, title: str, services: list[str], fault_type: str,
               severity: str, evidence: str, now: datetime,
               ticket_id: str | None = None) -> Ticket: ...

    @abstractmethod
    def get(self, ticket_id: str) -> Ticket | None: ...

    @abstractmethod
    def find_open_by_fingerprint(self, fingerprint: str) -> Ticket | None: ...

    @abstractmethod
    def add_comment(self, ticket_id: str, *, author: str, body: str, now: datetime) -> None: ...

    @abstractmethod
    def set_status(self, ticket_id: str, status: TicketStatus, *, now: datetime) -> None: ...

    @abstractmethod
    def assign(self, ticket_id: str, assignee: str, *, now: datetime) -> None: ...

    @abstractmethod
    def list_open(self) -> list[Ticket]: ...


class CompositeTicketStore(TicketStore):
    """Two ticket stores at once: a local store (authoritative — fast queries, full record,
    keeps working offline) and an external one (Jira) that mirrors it best-effort. A failure
    of the external store never blocks the agent. Both share the external's key when available,
    so every later comment/transition lands on the same ticket in both."""

    def __init__(self, local: TicketStore, external: TicketStore) -> None:
        self._local = local
        self._external = external

    def create(self, *, fingerprint: str, title: str, services: list[str], fault_type: str,
               severity: str, evidence: str, now: datetime,
               ticket_id: str | None = None) -> Ticket:
        ext_id: str | None = None
        try:
            ext_id = self._external.create(
                fingerprint=fingerprint, title=title, services=services, fault_type=fault_type,
                severity=severity, evidence=evidence, now=now).id
        except Exception:   # noqa: BLE001 — degrade to local-only if Jira is unreachable
            pass
        return self._local.create(
            fingerprint=fingerprint, title=title, services=services, fault_type=fault_type,
            severity=severity, evidence=evidence, now=now, ticket_id=ext_id)

    def add_comment(self, ticket_id: str, *, author: str, body: str, now: datetime) -> None:
        self._local.add_comment(ticket_id, author=author, body=body, now=now)
        self._mirror(lambda: self._external.add_comment(ticket_id, author=author, body=body, now=now))

    def set_status(self, ticket_id: str, status: TicketStatus, *, now: datetime) -> None:
        self._local.set_status(ticket_id, status, now=now)
        self._mirror(lambda: self._external.set_status(ticket_id, status, now=now))

    def assign(self, ticket_id: str, assignee: str, *, now: datetime) -> None:
        self._local.assign(ticket_id, assignee, now=now)
        self._mirror(lambda: self._external.assign(ticket_id, assignee, now=now))

    def get(self, ticket_id: str) -> Ticket | None:
        return self._local.get(ticket_id)

    def find_open_by_fingerprint(self, fingerprint: str) -> Ticket | None:
        return self._local.find_open_by_fingerprint(fingerprint)

    def list_open(self) -> list[Ticket]:
        return self._local.list_open()

    @staticmethod
    def _mirror(fn) -> None:
        try:
            fn()
        except Exception:   # noqa: BLE001 — Jira mirror is best-effort
            pass


class SqliteTicketStore(TicketStore):
    def __init__(self, path: str | Path) -> None:
        self._conn = connect(path)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY,
                seq INTEGER UNIQUE,
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
                ticket_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                author TEXT NOT NULL,
                body TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def _next_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM tickets").fetchone()
        return int(row["n"])

    def create(self, *, fingerprint: str, title: str, services: list[str], fault_type: str,
               severity: str, evidence: str, now: datetime,
               ticket_id: str | None = None) -> Ticket:
        seq = self._next_seq()
        ticket = Ticket(
            id=ticket_id or f"SRE-{seq}", fingerprint=fingerprint, title=title, services=services,
            fault_type=fault_type, severity=severity, status=TicketStatus.OPEN,
            evidence=evidence, created_at=now, updated_at=now,
        )
        self._conn.execute(
            "INSERT INTO tickets (id, seq, fingerprint, title, services, fault_type, "
            "severity, status, assignee, evidence, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket.id, seq, fingerprint, title, json.dumps(services), fault_type, severity,
             ticket.status.value, None, evidence, now.isoformat(), now.isoformat()),
        )
        self._conn.commit()
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        row = self._conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            return None
        comments = [
            Comment(ts=datetime.fromisoformat(c["ts"]), author=c["author"], body=c["body"])
            for c in self._conn.execute(
                "SELECT * FROM comments WHERE ticket_id = ? ORDER BY rowid", (ticket_id,))
        ]
        return self._row_to_ticket(row, comments)

    def find_open_by_fingerprint(self, fingerprint: str) -> Ticket | None:
        row = self._conn.execute(
            "SELECT id FROM tickets WHERE fingerprint = ? AND status != ? "
            "ORDER BY seq DESC LIMIT 1",
            (fingerprint, TicketStatus.RESOLVED.value),
        ).fetchone()
        return self.get(row["id"]) if row else None

    def add_comment(self, ticket_id: str, *, author: str, body: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO comments (ticket_id, ts, author, body) VALUES (?,?,?,?)",
            (ticket_id, now.isoformat(), author, body),
        )
        self._touch(ticket_id, now)
        self._conn.commit()

    def set_status(self, ticket_id: str, status: TicketStatus, *, now: datetime) -> None:
        self._conn.execute("UPDATE tickets SET status = ? WHERE id = ?",
                           (status.value, ticket_id))
        self._touch(ticket_id, now)
        self._conn.commit()

    def assign(self, ticket_id: str, assignee: str, *, now: datetime) -> None:
        self._conn.execute("UPDATE tickets SET assignee = ? WHERE id = ?",
                           (assignee, ticket_id))
        self._touch(ticket_id, now)
        self._conn.commit()

    def list_open(self) -> list[Ticket]:
        rows = self._conn.execute(
            "SELECT id FROM tickets WHERE status != ? ORDER BY seq",
            (TicketStatus.RESOLVED.value,),
        ).fetchall()
        return [t for t in (self.get(r["id"]) for r in rows) if t is not None]

    def _touch(self, ticket_id: str, now: datetime) -> None:
        self._conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?",
                           (now.isoformat(), ticket_id))

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row, comments: list[Comment]) -> Ticket:
        return Ticket(
            id=row["id"], fingerprint=row["fingerprint"], title=row["title"],
            services=json.loads(row["services"]), fault_type=row["fault_type"],
            severity=row["severity"], status=TicketStatus(row["status"]),
            assignee=row["assignee"], evidence=row["evidence"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            comments=comments,
        )
