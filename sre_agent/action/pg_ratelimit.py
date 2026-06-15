"""Fleet-wide atomic action rate limiter (Maturity 9 / ROADMAP P1.2).

`SqliteRateLimiter` makes the per-service restart cap atomic *within one host* (BEGIN IMMEDIATE
on a local file). For a multi-host fleet the cap must hold across replicas — two agents on two
machines must not both restart the same service to breach the cap, exactly when a runaway agent
is most dangerous. `PostgresRateLimiter` is that swap: the same `ActionRateLimiter` contract over
the shared change-log table, serialised with a Postgres transaction-scoped advisory lock keyed on
the target so concurrent callers across the fleet take turns, and the count-then-record is one
transaction. It writes the same `changes` table `PostgresChangeLog` reads, so a restart by any
replica counts against every replica's cap and is suppressed by every replica's detector (#5)."""
from __future__ import annotations

import zlib
from datetime import datetime, timedelta

from sre_agent.action.executor import Decision
from sre_agent.action.ratelimit import RATE_LIMITED
from sre_agent.pgdb import connect_pg

_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id BIGSERIAL PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, service TEXT NOT NULL,
    change_type TEXT NOT NULL, detail TEXT NOT NULL
)
"""


def _lock_key(action_type: str, service: str) -> int:
    """A stable signed-64-bit key for pg_advisory_xact_lock — same target ⇒ same key ⇒ callers
    serialise; different targets never block each other."""
    h = zlib.crc32(f"{action_type}:{service}".encode())
    return h - (1 << 31)  # center into signed range (deterministic, collision-free for crc32)


class PostgresRateLimiter:
    def __init__(self, max_restarts_per_hour: int, dsn: str,
                 window_s: float = 3600.0, actor: str = "sre-agent") -> None:
        self._max = max_restarts_per_hour
        self._window = timedelta(seconds=window_s)
        self._actor = actor
        self._conn = connect_pg(dsn)
        self._conn.execute(_SCHEMA)

    def try_consume(self, action_type: str, service: str, now: datetime,
                    detail: str = "") -> Decision:
        cutoff = (now - self._window).isoformat()
        with self._conn.transaction():
            # serialise every fleet caller acting on this target until this txn commits
            self._conn.execute("SELECT pg_advisory_xact_lock(%s)",
                               (_lock_key(action_type, service),))
            if action_type in RATE_LIMITED:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM changes WHERE actor=%s AND change_type=%s "
                    "AND service=%s AND ts>=%s",
                    (self._actor, action_type, service, cutoff)).fetchone()
                count = int(row["n"])
                if count >= self._max:
                    return Decision(False, f"restart cap reached for {service} "
                                           f"({count}/{self._max} in last hour)")
            self._conn.execute(
                "INSERT INTO changes (ts, actor, service, change_type, detail) "
                "VALUES (%s,%s,%s,%s,%s)",
                (now.isoformat(), self._actor, service, action_type, detail))
        return Decision(True)
