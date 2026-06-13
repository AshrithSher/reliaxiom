"""Confluence Cloud post-mortem adapter (M7). Publishes a page per post-mortem to a space.

Confluence is a *publish target*, not the query source: incident memory (recent_for_fingerprint)
must be fast, so a local PostMortemStore stays authoritative and CompositePostMortemStore tees
each record to both. A Confluence outage never blocks the agent — publishing is best-effort.
"""
from __future__ import annotations

from html import escape

from sre_agent.integrations.jira import JiraClient  # a generic Atlassian REST client
from sre_agent.integrations.postmortems import PostMortem, PostMortemStore


class ConfluencePublisher:
    def __init__(self, client: JiraClient, space_key: str, parent_id: str | None = None) -> None:
        self.client = client
        self.space = space_key
        self.parent_id = parent_id

    def publish(self, pm: PostMortem) -> str:
        payload: dict = {
            "type": "page",
            "title": f"Post-mortem {pm.incident_id} — {pm.fingerprint}",
            "space": {"key": self.space},
            "body": {"storage": {"value": _storage_html(pm), "representation": "storage"}},
        }
        if self.parent_id:
            payload["ancestors"] = [{"id": self.parent_id}]
        resp = self.client.request("POST", "/wiki/rest/api/content", payload)
        return str(resp.get("id", ""))


class CompositePostMortemStore(PostMortemStore):
    """Stores locally (authoritative, queryable) and publishes to Confluence (best-effort)."""

    def __init__(self, primary: PostMortemStore, publisher: ConfluencePublisher) -> None:
        self.primary = primary
        self.publisher = publisher

    def record(self, pm: PostMortem) -> None:
        self.primary.record(pm)
        try:
            self.publisher.publish(pm)
        except Exception:   # noqa: BLE001 — never let a Confluence outage block the agent
            pass

    def recent_for_fingerprint(self, fingerprint: str, limit: int = 3) -> list[PostMortem]:
        return self.primary.recent_for_fingerprint(fingerprint, limit)

    def all(self) -> list[PostMortem]:
        return self.primary.all()


def _storage_html(pm: PostMortem) -> str:
    mttr = f"{pm.time_to_resolve_s:.0f}s" if pm.time_to_resolve_s is not None else "n/a"
    timeline = "".join(f"<li>{escape(line)}</li>" for line in pm.timeline) or "<li>(none)</li>"
    return (
        f"<p><strong>Ticket:</strong> {escape(pm.ticket_id or 'n/a')} &middot; "
        f"<strong>Outcome:</strong> {escape(pm.outcome)} &middot; "
        f"<strong>Time to resolve:</strong> {mttr}</p>"
        f"<h2>Root cause</h2><p>{escape(pm.root_cause)}</p>"
        f"<h2>Action taken</h2><p>{escape(pm.action_taken)}</p>"
        f"<h2>Affected services</h2><p>{escape(', '.join(pm.services))}</p>"
        f"<h2>Timeline</h2><ul>{timeline}</ul>"
    )
