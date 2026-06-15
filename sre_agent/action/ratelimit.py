"""Fleet-safe action rate limiting (ROADMAP P0.5).

The per-service restart cap is a *safety* invariant, not a nicety — so it must hold across
every process acting on the system, not within one. The old design counted the change log in
`Guardrails` and then, in a separate write, recorded the action tag: a check-then-act gap two
replicas (or two ticks) could both slip through, so the fleet exceeded the cap exactly during
a fault, when a runaway agent is most dangerous.

`ActionRateLimiter` closes that gap. `try_consume` performs the count **and** records the
action's change-log tag (the invariant-#5 marker detection keys off) in **one atomic
transaction**, so concurrent callers serialize and the cap is exact. The default
`SqliteRateLimiter` uses a `BEGIN IMMEDIATE` transaction on the shared change-log DB, which is
correct for multiple processes on one host. A `PostgresRateLimiter` (the same contract, a
`SELECT ... FOR UPDATE` over a shared managed store) is the later swap for true multi-host
fleets (ROADMAP P1.2) — the engine only knows this Protocol."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from sre_agent.action.executor import Decision
from sre_agent.db import connect

# Action types subject to the per-service cap. Others are tagged but never blocked.
RATE_LIMITED: frozenset[str] = frozenset({"restart_container"})


@runtime_checkable
class ActionRateLimiter(Protocol):
    def try_consume(self, action_type: str, service: str, now: datetime,
                    detail: str = "") -> Decision:
        """Atomically: if `action_type` is rate-limited and the count in the trailing window
        is at/over the cap, return blocked and record nothing. Otherwise record the action
        (the change-log tag) and return allowed. Check-and-record is one transaction."""


_CREATE = """
CREATE TABLE IF NOT EXISTS changes (
    ts TEXT NOT NULL, actor TEXT NOT NULL, service TEXT NOT NULL,
    change_type TEXT NOT NULL, detail TEXT NOT NULL
)
"""


class SqliteRateLimiter:
    """Atomic per-host limiter over the shared change-log DB. Writes the same `changes` table
    `ChangeLog` does, so it counts restarts recorded by any path (auto or approved) and its
    tags are visible to diagnosis and to detection-suppression (#5)."""

    def __init__(self, max_restarts_per_hour: int, path: str | Path,
                 window_s: float = 3600.0, actor: str = "sre-agent") -> None:
        self._max = max_restarts_per_hour
        self._window = timedelta(seconds=window_s)
        self._actor = actor
        self._conn = connect(path)
        # explicit-transaction mode: our BEGIN IMMEDIATE / COMMIT are honored literally,
        # giving an atomic check-and-record instead of sqlite3's implicit autocommit.
        self._conn.isolation_level = None
        self._conn.execute(_CREATE)

    def try_consume(self, action_type: str, service: str, now: datetime,
                    detail: str = "") -> Decision:
        cutoff = (now - self._window).isoformat()
        self._conn.execute("BEGIN IMMEDIATE")  # take the write lock now → callers serialize
        try:
            if action_type in RATE_LIMITED:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM changes WHERE actor=? AND change_type=? "
                    "AND service=? AND ts>=?",
                    (self._actor, action_type, service, cutoff)).fetchone()
                count = row[0]
                if count >= self._max:
                    self._conn.execute("ROLLBACK")
                    return Decision(False, f"restart cap reached for {service} "
                                           f"({count}/{self._max} in last hour)")
            self._conn.execute(
                "INSERT INTO changes (ts, actor, service, change_type, detail) "
                "VALUES (?,?,?,?,?)",
                (now.isoformat(), self._actor, service, action_type, detail))
            self._conn.execute("COMMIT")
            return Decision(True)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
