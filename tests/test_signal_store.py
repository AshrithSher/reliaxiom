"""TDD: SignalStore and the poll-once cycle. The store holds the latest snapshot; a poll
cycle runs every poller, merges their partial results, and must isolate a failing poller
so one broken source doesn't blank out the others."""
from datetime import datetime, timezone

from sre_agent.models import ContainerState
from sre_agent.poll.store import PollCycle, SignalStore

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakePoller:
    def __init__(self, result, raises=False):
        self._result = result
        self._raises = raises

    def poll(self):
        if self._raises:
            raise RuntimeError("poller blew up")
        return self._result


def test_store_starts_empty():
    store = SignalStore()
    assert store.latest() is None


def test_poll_cycle_merges_all_pollers():
    store = SignalStore()
    cycle = PollCycle([
        FakePoller({"containers": {"api": ContainerState(name="api", status="running")}}),
        FakePoller({"redis_queue_depth": 5}),
        FakePoller({"pg_connections": 9}),
    ], store)
    cycle.run_once(NOW)
    snap = store.latest()
    assert snap is not None
    assert snap.ts == NOW
    assert snap.containers["api"].status == "running"
    assert snap.redis_queue_depth == 5
    assert snap.pg_connections == 9


def test_one_failing_poller_does_not_sink_the_others():
    store = SignalStore()
    cycle = PollCycle([
        FakePoller({"redis_queue_depth": 5}),
        FakePoller(None, raises=True),        # this one explodes
        FakePoller({"pg_connections": 9}),
    ], store)
    cycle.run_once(NOW)
    snap = store.latest()
    assert snap.redis_queue_depth == 5 and snap.pg_connections == 9


def test_latest_returns_most_recent_cycle():
    store = SignalStore()
    cycle = PollCycle([FakePoller({"redis_queue_depth": 1})], store)
    cycle.run_once(NOW)
    later = NOW.replace(second=30)
    cycle = PollCycle([FakePoller({"redis_queue_depth": 99})], store)
    cycle.run_once(later)
    assert store.latest().redis_queue_depth == 99
    assert store.latest().ts == later
