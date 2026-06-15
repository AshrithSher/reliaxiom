"""TDD: HA coordination (Maturity 9 / D-041) — leader election and the fleet-wide atomic
restart cap. The single-node leader is always pure logic; the Postgres pieces are exercised
live when $SRE_STATE_DSN is set (skipped otherwise)."""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

import pytest

from sre_agent.ha.leader import SingleNodeLeadership

T0 = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
_DSN = os.environ.get("SRE_STATE_DSN", "")
_pg = pytest.mark.skipif(not _DSN, reason="set SRE_STATE_DSN to exercise Postgres coordination")


def test_single_node_is_always_leader():
    lead = SingleNodeLeadership()
    assert lead.acquire() is True
    assert lead.is_leader is True
    lead.release()


@_pg
def test_only_one_replica_wins_leadership():
    from sre_agent.ha.leader import PostgresLeadership
    a = PostgresLeadership(_DSN, lock_key=4242, replica_id="a")
    b = PostgresLeadership(_DSN, lock_key=4242, replica_id="b")
    try:
        assert a.acquire() is True          # first to contend wins
        assert b.acquire() is False         # the other cannot
        assert a.acquire() is True          # re-acquiring a held lock is idempotent
    finally:
        a.release()
        b.release()


@_pg
def test_leadership_fails_over_when_leader_releases():
    from sre_agent.ha.leader import PostgresLeadership
    a = PostgresLeadership(_DSN, lock_key=4243, replica_id="a")
    b = PostgresLeadership(_DSN, lock_key=4243, replica_id="b")
    try:
        assert a.acquire() is True
        assert b.acquire() is False
        a.release()                          # leader dies → lock auto-released
        assert b.acquire() is True           # standby takes over
    finally:
        a.release()
        b.release()


@_pg
def test_postgres_rate_limiter_cap_holds_under_concurrency():
    # the cap is a SAFETY invariant: with N threads all trying to restart the same service and
    # cap=3, EXACTLY 3 must succeed — never more, even under contention (the check-then-act race
    # the per-host limiter closes within a host, this closes across the fleet).
    from sre_agent.action.pg_ratelimit import PostgresRateLimiter
    from sre_agent.pgdb import connect_pg
    conn = connect_pg(_DSN)
    conn.execute("DROP TABLE IF EXISTS changes CASCADE")
    conn.close()

    cap = 3
    limiters = [PostgresRateLimiter(cap, _DSN) for _ in range(20)]
    results: list[bool] = []
    lock = threading.Lock()

    def worker(rl):
        ok = rl.try_consume("restart_container", "api", T0, detail="x").allowed
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker, args=(rl,)) for rl in limiters]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == cap            # exactly cap succeed, never more
    # a different service is independent — its own cap budget
    assert PostgresRateLimiter(cap, _DSN).try_consume("restart_container", "redis", T0).allowed
