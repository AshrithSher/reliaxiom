"""The change log: a timestamped record of deployments, config changes, and (from M4) the
agent's own actions. Two consumers: the diagnosis context (what changed before symptoms)
and, in M4, detection (ignore anomalies inside a tagged agent action's blast radius —
invariant #5, 'the agent must not diagnose its own remediation')."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class ChangeLogEntry(BaseModel):
    ts: datetime
    actor: str           # "deployer", "sre-agent", "operator", ...
    service: str
    change_type: str     # "deploy", "config", "restart_container", ...
    detail: str = ""


class ChangeLog:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS changes (
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                service TEXT NOT NULL,
                change_type TEXT NOT NULL,
                detail TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def record(self, entry: ChangeLogEntry) -> None:
        self._conn.execute(
            "INSERT INTO changes (ts, actor, service, change_type, detail) VALUES (?,?,?,?,?)",
            (entry.ts.isoformat(), entry.actor, entry.service, entry.change_type, entry.detail),
        )
        self._conn.commit()

    def recent(self, since: datetime, until: datetime) -> list[ChangeLogEntry]:
        rows = self._conn.execute(
            "SELECT * FROM changes WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (since.isoformat(), until.isoformat()),
        ).fetchall()
        return [ChangeLogEntry(ts=datetime.fromisoformat(r["ts"]), actor=r["actor"],
                               service=r["service"], change_type=r["change_type"],
                               detail=r["detail"]) for r in rows]
