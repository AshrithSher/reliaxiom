"""TDD: CompositeTicketStore — Jira alongside SQLite. The local store is authoritative
(queries, full record); the external store mirrors best-effort and its failures never block
the agent. Both share the external's key so later comments/transitions hit the same ticket."""
from datetime import datetime, timezone

from sre_agent.integrations.ticketing import (
    CompositeTicketStore,
    SqliteTicketStore,
    TicketStatus,
    TicketStore,
    Ticket,
)

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeExternal(TicketStore):
    """Stand-in for Jira: assigns keys like HELP-N and records calls; can be made to fail."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self._n = 0

    def create(self, *, fingerprint, title, services, fault_type, severity, evidence, now,
               ticket_id=None):
        if self.fail:
            raise RuntimeError("jira down")
        self._n += 1
        self.calls.append(("create", f"HELP-{self._n}"))
        return Ticket(id=f"HELP-{self._n}", fingerprint=fingerprint, title=title,
                      services=services, fault_type=fault_type, severity=severity,
                      status=TicketStatus.OPEN, created_at=now, updated_at=now)

    def add_comment(self, ticket_id, *, author, body, now):
        if self.fail:
            raise RuntimeError("jira down")
        self.calls.append(("comment", ticket_id, body))

    def set_status(self, ticket_id, status, *, now):
        if self.fail:
            raise RuntimeError("jira down")
        self.calls.append(("status", ticket_id, status))

    def assign(self, ticket_id, assignee, *, now):
        self.calls.append(("assign", ticket_id, assignee))

    def get(self, ticket_id):
        return None

    def find_open_by_fingerprint(self, fingerprint):
        return None

    def list_open(self):
        return []


def make(tmp_path, fail=False):
    local = SqliteTicketStore(tmp_path / "t.db")
    ext = FakeExternal(fail=fail)
    return CompositeTicketStore(local, ext), local, ext


def create(store):
    return store.create(fingerprint="redis:unreachable", title="redis", services=["api"],
                        fault_type="unreachable", severity="High", evidence="report", now=NOW)


def test_create_writes_both_and_shares_jira_key(tmp_path):
    store, local, ext = make(tmp_path)
    t = create(store)
    assert t.id == "HELP-1"                       # shares Jira's key
    assert local.get("HELP-1") is not None        # mirrored locally under the same id
    assert ("create", "HELP-1") in ext.calls


def test_comment_and_status_hit_both(tmp_path):
    store, local, ext = make(tmp_path)
    t = create(store)
    store.add_comment(t.id, author="agent", body="diagnosis posted", now=NOW)
    store.set_status(t.id, TicketStatus.RESOLVED, now=NOW)
    assert local.get(t.id).comments[0].body == "diagnosis posted"     # local
    assert ("comment", "HELP-1", "diagnosis posted") in ext.calls     # external
    assert ("status", "HELP-1", TicketStatus.RESOLVED) in ext.calls


def test_external_failure_does_not_block_agent(tmp_path):
    store, local, ext = make(tmp_path, fail=True)
    t = create(store)                              # Jira create failed → local-only id
    assert t.id == "SRE-1"                          # fell back to local sequence
    store.add_comment(t.id, author="agent", body="still works", now=NOW)   # must not raise
    assert local.get("SRE-1").comments[0].body == "still works"


def test_queries_use_local(tmp_path):
    store, local, ext = make(tmp_path)
    create(store)
    assert store.find_open_by_fingerprint("redis:unreachable").id == "HELP-1"
    assert len(store.list_open()) == 1
