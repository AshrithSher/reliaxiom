"""TDD: the fleet-safe action rate limiter.

The per-service restart cap used to be counted in `Guardrails` by reading a local SQLite
change log and *then*, separately, the manager wrote the tag — a check-then-act gap that two
processes (or two ticks) could both pass, so the fleet exceeded the cap. The limiter closes
that: counting the trailing window and recording the action (the invariant-#5 tag) happen in
**one atomic transaction**, so concurrent callers serialize and the cap holds across
processes. Non-rate-limited actions are always allowed but still tagged."""
import threading
from datetime import datetime, timedelta, timezone

from sre_agent.action.ratelimit import SqliteRateLimiter
from sre_agent.changelog import ChangeLog
from sre_agent.db import connect

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def _count(path, service, action_type="restart_container"):
    conn = connect(path)
    row = conn.execute(
        "SELECT COUNT(*) FROM changes WHERE actor='sre-agent' AND change_type=? AND service=?",
        (action_type, service)).fetchone()
    return row[0]


def test_allows_and_tags_under_cap(tmp_path):
    db = tmp_path / "changes.db"
    rl = SqliteRateLimiter(max_restarts_per_hour=3, path=db)
    d = rl.try_consume("restart_container", "worker", NOW, detail="{'service': 'worker'}")
    assert d.allowed
    assert _count(db, "worker") == 1     # the action was recorded (the #5 tag)


def test_blocks_at_cap_without_tagging(tmp_path):
    db = tmp_path / "changes.db"
    rl = SqliteRateLimiter(max_restarts_per_hour=3, path=db)
    for _ in range(3):
        assert rl.try_consume("restart_container", "worker", NOW).allowed
    blocked = rl.try_consume("restart_container", "worker", NOW)
    assert not blocked.allowed and "cap" in blocked.reason.lower()
    assert _count(db, "worker") == 3     # blocked call did NOT record a 4th


def test_cap_is_per_service(tmp_path):
    db = tmp_path / "changes.db"
    rl = SqliteRateLimiter(max_restarts_per_hour=3, path=db)
    for _ in range(3):
        rl.try_consume("restart_container", "worker", NOW)
    assert rl.try_consume("restart_container", "api", NOW).allowed   # different service


def test_old_actions_outside_window_dont_count(tmp_path):
    db = tmp_path / "changes.db"
    # pre-load 5 restarts two hours ago via the change log the limiter reads
    log = ChangeLog(db)
    from sre_agent.changelog import ChangeLogEntry
    for _ in range(5):
        log.record(ChangeLogEntry(ts=NOW - timedelta(hours=2), actor="sre-agent",
                                  service="worker", change_type="restart_container", detail="x"))
    rl = SqliteRateLimiter(max_restarts_per_hour=3, path=db)
    assert rl.try_consume("restart_container", "worker", NOW).allowed


def test_non_rate_limited_action_always_allowed_and_tagged(tmp_path):
    db = tmp_path / "changes.db"
    rl = SqliteRateLimiter(max_restarts_per_hour=1, path=db)
    for _ in range(5):
        assert rl.try_consume("rerun_failed_job", "worker", NOW).allowed
    assert _count(db, "worker", "rerun_failed_job") == 5    # not capped, but tagged


def test_concurrent_consumers_never_exceed_cap(tmp_path):
    """The race the old per-process check-then-act lost: many threads against one store must
    not collectively exceed the cap. With the atomic BEGIN IMMEDIATE count+insert, exactly
    `cap` succeed."""
    db = tmp_path / "changes.db"
    cap = 5
    ChangeLog(db)   # create the store + set WAL once at startup, as the agent does in main()
    # one limiter per thread, all pointing at the same DB file (stands in for replicas)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        rl = SqliteRateLimiter(max_restarts_per_hour=cap, path=db)
        ok = rl.try_consume("restart_container", "worker", NOW).allowed
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == cap            # exactly cap allowed, no more
    assert _count(db, "worker") == cap    # and exactly cap tags written
