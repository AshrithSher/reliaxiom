"""Leader election so multiple agent replicas never double-act on one incident.

The HA model is leader-election (one acts, others hot-standby), not work-partitioning: every
replica tails + detects (keeping its window warm for fast failover), but only the *leader* drives
the incident lifecycle — correlate → ticket → diagnose → act → verify. Because incident state
lives in the shared store, a standby that wins leadership after the leader dies resumes
mid-incident without re-executing actions (D-009 at fleet scale; actions are idempotent anyway).

`PostgresLeadership` uses a *session-scoped* advisory lock (`pg_try_advisory_lock`). This is the
right primitive precisely because of its failure semantics: the lock is held on a dedicated
connection and auto-released the instant that connection drops — so if the leader process or host
dies, Postgres releases the lock and a standby acquires it on its next attempt, with no lease
timer to tune and no split-brain window beyond one tick. `acquire()` is called every tick; it is
idempotent (re-acquiring a held lock returns True) and self-heals a dropped connection.

`SingleNodeLeadership` is the default (HA off): always the leader, zero dependencies — so the
single-process path and every offline test are unchanged."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Leadership(Protocol):
    def acquire(self) -> bool:
        """Try to become / remain the leader. Returns whether this replica is now the leader."""

    @property
    def is_leader(self) -> bool: ...

    def release(self) -> None: ...


class SingleNodeLeadership:
    """No coordination — this process is always the leader. The non-HA default."""

    is_leader = True

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None


class PostgresLeadership:
    """One leader across the fleet via a session-scoped Postgres advisory lock."""

    def __init__(self, dsn: str, lock_key: int = 911, replica_id: str = "") -> None:
        self._dsn = dsn
        self._key = lock_key
        self.replica_id = replica_id
        self._is_leader = False
        self._conn = None
        self._connect()

    def _connect(self) -> None:
        from sre_agent.pgdb import connect_pg
        self._conn = connect_pg(self._dsn)
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def acquire(self) -> bool:
        try:
            if self._is_leader:
                # confirm we still hold it (connection alive). A dropped connection means the
                # lock was already released — fall through to re-contend.
                self._conn.execute("SELECT 1")
                return True
            row = self._conn.execute("SELECT pg_try_advisory_lock(%s) AS got",
                                     (self._key,)).fetchone()
            self._is_leader = bool(row["got"])
            return self._is_leader
        except Exception:  # noqa: BLE001 — a DB blip must not crash the loop; reconnect & retry next tick
            try:
                self._connect()
            except Exception:  # noqa: BLE001
                pass
            self._is_leader = False
            return False

    def release(self) -> None:
        try:
            if self._conn is not None:
                if self._is_leader:
                    self._conn.execute("SELECT pg_advisory_unlock(%s)", (self._key,))
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._is_leader = False
