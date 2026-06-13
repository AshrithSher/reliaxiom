"""TDD: the Jira ticketing adapter — request shaping and field mapping, exercised with an
injected fake transport so no live Jira is touched. Live connectivity is a separate smoke."""
import json
from datetime import datetime, timezone

import pytest

from sre_agent.integrations.jira import JiraClient, JiraError, JiraTicketStore, _label
from sre_agent.integrations.ticketing import TicketStatus

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeTransport:
    """Records requests; returns a canned (status, body) per (method, path-substring)."""

    def __init__(self, routes):
        self.routes = routes              # list of (method, substr, status, body_dict)
        self.calls = []                   # (method, url, parsed_body)

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, json.loads(body) if body else None))
        for m, substr, status, payload in self.routes:
            if m == method and substr in url:
                return status, json.dumps(payload)
        return 404, '{"errorMessages":["no route"]}'

    def last_body(self, substr):
        return next(b for (m, u, b) in reversed(self.calls) if substr in u)


def store(routes):
    t = FakeTransport(routes)
    client = JiraClient("https://x.atlassian.net", "me@x.com", "tok", transport=t)
    return JiraTicketStore(client, project_key="HELP", oncall_account_id="acc-123"), t


def test_create_posts_incident_with_mapping():
    s, t = store([("POST", "/issue", 201, {"key": "HELP-7"})])
    ticket = s.create(fingerprint="redis:unreachable", title="redis down", services=["api", "worker"],
                      fault_type="unreachable", severity="High",
                      evidence="initial report: redis maxmemory exhausted", now=NOW)
    assert ticket.id == "HELP-7" and ticket.status is TicketStatus.OPEN
    fields = t.last_body("/issue")["fields"]
    assert fields["project"]["key"] == "HELP"
    assert fields["issuetype"]["name"] == "[System] Incident"
    assert fields["summary"] == "redis down"
    assert fields["priority"]["name"] == "High"
    assert fields["labels"] == ["sre-redis-unreachable"]
    # the caller's full report becomes the description (ADF)
    assert "maxmemory exhausted" in json.dumps(fields["description"])


def test_label_sanitizes_fingerprint():
    assert _label("api:internal_error") == "sre-api-internal-error"
    assert _label("redis:unreachable") == "sre-redis-unreachable"


def test_add_comment_uses_adf_and_author():
    s, t = store([("POST", "/comment", 201, {})])
    s.add_comment("HELP-7", author="agent", body="diagnosis posted", now=NOW)
    body = t.last_body("/comment")
    assert "diagnosis posted" in json.dumps(body["body"])
    assert "agent" in json.dumps(body["body"])


def test_set_status_finds_and_executes_transition():
    s, t = store([
        ("GET", "fields=status", 200, {"fields": {"status": {"name": "Open"}}}),
        ("GET", "/transitions", 200, {"transitions": [
            {"id": "11", "to": {"name": "Open"}},
            {"id": "21", "to": {"name": "Work in progress"}}]}),
        ("POST", "/transitions", 204, {}),
    ])
    s.set_status("HELP-7", TicketStatus.ACTING, now=NOW)   # ACTING → "Work in progress"
    posted = [c for c in t.calls if c[0] == "POST" and "/transitions" in c[1]]
    assert posted and posted[-1][2] == {"transition": {"id": "21"}}


def test_set_status_no_forward_transition_is_noop():
    # at "Work in progress" with only a backward transition (Open); target Completed unreachable
    s, t = store([
        ("GET", "fields=status", 200, {"fields": {"status": {"name": "Work in progress"}}}),
        ("GET", "/transitions", 200, {"transitions": [{"id": "11", "to": {"name": "Open"}}]})])
    s.set_status("HELP-7", TicketStatus.RESOLVED, now=NOW)
    assert not [c for c in t.calls if c[0] == "POST"]        # didn't step backward to Open


class StatefulWorkflow:
    """A fake Jira that tracks the issue's current status and returns transitions for it."""

    GRAPH = {                                  # status -> [(transition_id, to_status)]
        "Open": [("11", "Pending"), ("12", "Work in progress")],
        "Pending": [("21", "Work in progress")],
        "Work in progress": [("31", "Completed")],
        "Completed": [],
    }

    def __init__(self, start):
        self.status = start
        self.transitioned = []

    def __call__(self, method, url, headers, body):
        if "fields=status" in url:
            return 200, json.dumps({"fields": {"status": {"name": self.status}}})
        if "/transitions" in url and method == "GET":
            trs = [{"id": i, "to": {"name": s}} for i, s in self.GRAPH[self.status]]
            return 200, json.dumps({"transitions": trs})
        if "/transitions" in url and method == "POST":
            tid = json.loads(body)["transition"]["id"]
            to = next(s for i, s in self.GRAPH[self.status] if i == tid)
            self.status = to
            self.transitioned.append(to)
            return 204, ""
        return 404, "{}"


def test_set_status_walks_multiple_hops():
    wf = StatefulWorkflow(start="Pending")     # approval state
    s = JiraTicketStore(JiraClient("https://x", "e", "t", transport=wf), "HELP")
    s.set_status("HELP-7", TicketStatus.RESOLVED, now=NOW)   # → "Completed", two hops away
    assert wf.transitioned == ["Work in progress", "Completed"]
    assert wf.status == "Completed"


def test_assign_uses_oncall_account():
    s, t = store([("PUT", "/assignee", 204, {})])
    s.assign("HELP-7", "oncall@example.com", now=NOW)
    assert t.last_body("/assignee") == {"accountId": "acc-123"}


def test_find_open_by_fingerprint_builds_jql():
    s, t = store([("POST", "/search", 200, {"issues": [
        {"key": "HELP-7", "fields": {"summary": "x", "status": {"name": "Open"},
                                     "labels": ["sre-redis-unreachable"]}}]})])
    found = s.find_open_by_fingerprint("redis:unreachable")
    assert found.id == "HELP-7" and found.fingerprint == "redis:unreachable"
    jql = t.last_body("/search")["jql"]
    assert "labels = \"sre-redis-unreachable\"" in jql and "statusCategory != Done" in jql


def test_find_open_returns_none_when_empty():
    s, _ = store([("POST", "/search", 200, {"issues": []})])
    assert s.find_open_by_fingerprint("nothing:here") is None


def test_client_raises_on_http_error():
    s, _ = store([("POST", "/issue", 400, {"errorMessages": ["bad"]})])
    with pytest.raises(JiraError):
        s.create(fingerprint="x:y", title="t", services=[], fault_type="y", severity="Low",
                 evidence="", now=NOW)
