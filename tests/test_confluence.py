"""TDD: the Confluence post-mortem adapter — page shaping + the composite that keeps local
memory authoritative while publishing to Confluence best-effort."""
import json
from datetime import datetime, timezone

from sre_agent.integrations.confluence import ConfluencePublisher, CompositePostMortemStore
from sre_agent.integrations.jira import JiraClient
from sre_agent.integrations.postmortems import PostMortem, SqlitePostMortemStore

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def pm():
    return PostMortem(
        incident_id="INC-1", ticket_id="HELP-7", fingerprint="redis:unreachable",
        root_service="redis", services=["api", "worker"], root_cause="redis maxmemory exhausted",
        action_taken="restart_container redis", outcome="resolved", time_to_resolve_s=142.0,
        created_at=NOW, timeline=["detected", "diagnosed", "resolved"])


class FakeTransport:
    def __init__(self, status=200, payload=None):
        self.status, self.payload = status, payload or {"id": "98765"}
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, json.loads(body) if body else None))
        return self.status, json.dumps(self.payload)


def publisher(transport):
    return ConfluencePublisher(JiraClient("https://x.atlassian.net", "me@x.com", "t", transport=transport),
                               space_key="ReliAxiom")


def test_publish_posts_page_to_space():
    t = FakeTransport()
    page_id = publisher(t).publish(pm())
    assert page_id == "98765"
    method, url, body = t.calls[-1]
    assert method == "POST" and "/wiki/rest/api/content" in url
    assert body["space"]["key"] == "ReliAxiom"
    assert body["title"] == "Post-mortem INC-1 — redis:unreachable"
    assert "redis maxmemory exhausted" in body["body"]["storage"]["value"]
    assert body["body"]["storage"]["representation"] == "storage"


def test_publish_under_parent_when_configured():
    t = FakeTransport()
    p = ConfluencePublisher(JiraClient("https://x", "e", "t", transport=t), "ReliAxiom", parent_id="111")
    p.publish(pm())
    assert t.calls[-1][2]["ancestors"] == [{"id": "111"}]


def test_composite_records_locally_and_publishes(tmp_path):
    local = SqlitePostMortemStore(tmp_path / "pm.db")
    t = FakeTransport()
    store = CompositePostMortemStore(local, publisher(t))
    store.record(pm())
    # local is authoritative + queryable
    assert store.recent_for_fingerprint("redis:unreachable")[0].incident_id == "INC-1"
    # and it published to Confluence
    assert any("/wiki/rest/api/content" in c[1] for c in t.calls)


def test_composite_survives_confluence_outage(tmp_path):
    local = SqlitePostMortemStore(tmp_path / "pm.db")
    store = CompositePostMortemStore(local, publisher(FakeTransport(status=503, payload={"e": 1})))
    store.record(pm())   # must not raise
    assert store.recent_for_fingerprint("redis:unreachable")   # local write still succeeded
