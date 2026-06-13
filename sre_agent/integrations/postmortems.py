"""Post-mortem integration boundary (M6). A resolved/escalated incident produces a
PostMortem: structured (queryable by fingerprint — the incident-memory read path) and
rendered to markdown (the human artifact; the Confluence adapter swaps in at M7)."""
from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class PostMortem(BaseModel):
    incident_id: str
    ticket_id: str | None
    fingerprint: str
    root_service: str
    services: list[str]
    root_cause: str
    action_taken: str
    outcome: str                 # "resolved" | "escalated"
    time_to_resolve_s: float | None
    created_at: datetime
    timeline: list[str] = Field(default_factory=list)


class PostMortemStore(ABC):
    @abstractmethod
    def record(self, pm: PostMortem) -> None: ...

    @abstractmethod
    def recent_for_fingerprint(self, fingerprint: str, limit: int = 3) -> list[PostMortem]: ...

    @abstractmethod
    def all(self) -> list[PostMortem]: ...


class SqlitePostMortemStore(PostMortemStore):
    def __init__(self, path: str | Path, markdown_dir: str | Path | None = None) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS postmortems (
                incident_id TEXT PRIMARY KEY,
                ticket_id TEXT,
                fingerprint TEXT NOT NULL,
                root_service TEXT NOT NULL,
                services TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                action_taken TEXT NOT NULL,
                outcome TEXT NOT NULL,
                time_to_resolve_s REAL,
                created_at TEXT NOT NULL,
                timeline TEXT NOT NULL
            )
            """
        )
        self._conn.commit()
        self._md_dir = Path(markdown_dir) if markdown_dir else None

    def record(self, pm: PostMortem) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO postmortems (incident_id, ticket_id, fingerprint, "
            "root_service, services, root_cause, action_taken, outcome, time_to_resolve_s, "
            "created_at, timeline) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pm.incident_id, pm.ticket_id, pm.fingerprint, pm.root_service,
             json.dumps(pm.services), pm.root_cause, pm.action_taken, pm.outcome,
             pm.time_to_resolve_s, pm.created_at.isoformat(), json.dumps(pm.timeline)),
        )
        self._conn.commit()
        if self._md_dir is not None:
            self._md_dir.mkdir(parents=True, exist_ok=True)
            (self._md_dir / f"{pm.incident_id}.md").write_text(render_markdown(pm),
                                                               encoding="utf-8")

    def recent_for_fingerprint(self, fingerprint: str, limit: int = 3) -> list[PostMortem]:
        rows = self._conn.execute(
            "SELECT * FROM postmortems WHERE fingerprint = ? ORDER BY created_at DESC LIMIT ?",
            (fingerprint, limit)).fetchall()
        return [self._from_row(r) for r in rows]

    def all(self) -> list[PostMortem]:
        rows = self._conn.execute(
            "SELECT * FROM postmortems ORDER BY created_at DESC").fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PostMortem:
        return PostMortem(
            incident_id=row["incident_id"], ticket_id=row["ticket_id"],
            fingerprint=row["fingerprint"], root_service=row["root_service"],
            services=json.loads(row["services"]), root_cause=row["root_cause"],
            action_taken=row["action_taken"], outcome=row["outcome"],
            time_to_resolve_s=row["time_to_resolve_s"],
            created_at=datetime.fromisoformat(row["created_at"]),
            timeline=json.loads(row["timeline"]))


def health_report(postmortems: list[PostMortem]) -> str:
    """A weekly-style health report from the post-mortem archive: volume, noisiest services,
    recurring fingerprints, and auto-resolve rate (the agent's effectiveness)."""
    if not postmortems:
        return "# Health report\n\nNo incidents recorded.\n"
    total = len(postmortems)
    resolved = sum(1 for p in postmortems if p.outcome == "resolved")
    by_service: dict[str, int] = {}
    by_fingerprint: dict[str, int] = {}
    for p in postmortems:
        by_fingerprint[p.fingerprint] = by_fingerprint.get(p.fingerprint, 0) + 1
        for svc in p.services:
            by_service[svc] = by_service.get(svc, 0) + 1
    recurring = sorted(((fp, n) for fp, n in by_fingerprint.items() if n > 1),
                       key=lambda x: -x[1])
    noisiest = sorted(by_service.items(), key=lambda x: -x[1])[:5]
    resolves = [p.time_to_resolve_s for p in postmortems if p.time_to_resolve_s is not None]
    mttr = f"{sum(resolves) / len(resolves):.0f}s" if resolves else "n/a"

    lines = [
        "# Health report", "",
        f"- Incidents: **{total}**  ·  auto-resolved: **{resolved}/{total}** "
        f"({100 * resolved // total}%)  ·  escalated: **{total - resolved}**",
        f"- Mean time to resolve (auto): **{mttr}**", "",
        "## Noisiest services",
        *[f"- {svc}: {n}" for svc, n in noisiest],
        "", "## Recurring faults",
        *([f"- {fp}: {n}×" for fp, n in recurring] or ["- (none recurred)"]),
    ]
    return "\n".join(lines) + "\n"


def render_markdown(pm: PostMortem) -> str:
    mttr = f"{pm.time_to_resolve_s:.0f}s" if pm.time_to_resolve_s is not None else "n/a"
    timeline = "\n".join(f"- {line}" for line in pm.timeline) or "- (none recorded)"
    return (
        f"# Post-mortem {pm.incident_id} — {pm.fingerprint}\n\n"
        f"- **Ticket:** {pm.ticket_id}\n"
        f"- **Outcome:** {pm.outcome}\n"
        f"- **Root service:** {pm.root_service}\n"
        f"- **Affected services:** {', '.join(pm.services)}\n"
        f"- **Time to resolve:** {mttr}\n"
        f"- **Date:** {pm.created_at.isoformat()}\n\n"
        f"## Root cause\n{pm.root_cause}\n\n"
        f"## Action taken\n{pm.action_taken}\n\n"
        f"## Timeline\n{timeline}\n"
    )
