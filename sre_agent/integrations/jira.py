"""Jira Cloud ticketing adapter (M7) — a TicketStore backed by the Jira REST API, swapping
in for the SQLite stub behind the same interface. HTTP transport is injected so the mapping
and request shaping are unit-tested without touching live Jira.

Maps our incident lifecycle onto the project's workflow (Open / Work in progress / Pending /
Completed) and our severity onto Jira priorities. Fingerprint is stored as a label for JQL
dedup. Comment/description bodies use Atlassian Document Format (ADF), required by API v3.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from sre_agent.integrations.ticketing import Comment, Ticket, TicketStatus, TicketStore

# transport(method, url, headers, body) -> (status_code, response_text)
Transport = Callable[[str, str, dict, str | None], tuple[int, str]]

INCIDENT_ISSUE_TYPE = "[System] Incident"

# our severity → Jira priority
_PRIORITY = {"High": "High", "Medium": "Medium", "Low": "Low"}

# our ticket status → Jira workflow status name
_TO_JIRA_STATUS = {
    TicketStatus.OPEN: "Open",
    TicketStatus.DIAGNOSING: "Work in progress",
    TicketStatus.ACTING: "Work in progress",
    TicketStatus.VERIFYING: "Work in progress",
    TicketStatus.AWAITING_APPROVAL: "Pending",
    TicketStatus.RESOLVED: "Completed",
    TicketStatus.ESCALATED: "Work in progress",
}

# progression rank used to walk multi-hop transitions toward a target status
_STATUS_RANK = {"open": 0, "pending": 1, "work in progress": 2,
                "completed": 3, "closed": 3, "cancelled": 3}

# Jira status name → our ticket status (best-effort reverse; ambiguity is fine because the
# manager tracks authoritative state in the IncidentStore, not the ticket)
_FROM_JIRA_STATUS = {
    "open": TicketStatus.OPEN,
    "work in progress": TicketStatus.ACTING,
    "pending": TicketStatus.AWAITING_APPROVAL,
    "completed": TicketStatus.RESOLVED,
    "closed": TicketStatus.RESOLVED,
    "cancelled": TicketStatus.RESOLVED,
}


class JiraError(Exception):
    pass


class JiraClient:
    def __init__(self, site: str, email: str, token: str,
                 transport: Transport | None = None, timeout_s: float = 20.0) -> None:
        self.site = site.rstrip("/")
        self._auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._transport = transport or _urllib_transport
        self._timeout = timeout_s

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = self.site + path
        headers = {"Authorization": f"Basic {self._auth}", "Accept": "application/json",
                   "Content-Type": "application/json"}
        data = json.dumps(body) if body is not None else None
        try:
            status, text = self._transport(method, url, headers, data)
        except Exception as exc:  # noqa: BLE001
            raise JiraError(f"{method} {path} transport error: {exc}") from exc
        if status >= 400:
            raise JiraError(f"{method} {path} -> HTTP {status}: {text[:300]}")
        return json.loads(text) if text.strip() else {}


class JiraTicketStore(TicketStore):
    def __init__(self, client: JiraClient, project_key: str,
                 oncall_account_id: str | None = None) -> None:
        self.client = client
        self.project = project_key
        self.oncall = oncall_account_id

    def create(self, *, fingerprint: str, title: str, services: list[str], fault_type: str,
               severity: str, evidence: str, now: datetime,
               ticket_id: str | None = None) -> Ticket:  # ticket_id unused — Jira assigns the key
        description = evidence   # the manager provides the full initial incident report
        fields = {
            "project": {"key": self.project},
            "issuetype": {"name": INCIDENT_ISSUE_TYPE},
            "summary": title[:250],
            "description": _adf(description),
            "priority": {"name": _PRIORITY.get(severity, "Medium")},
            "labels": [_label(fingerprint)],
        }
        resp = self.client.request("POST", "/rest/api/3/issue", {"fields": fields})
        return Ticket(id=resp["key"], fingerprint=fingerprint, title=title, services=services,
                      fault_type=fault_type, severity=severity, status=TicketStatus.OPEN,
                      evidence=evidence, created_at=now, updated_at=now)

    def add_comment(self, ticket_id: str, *, author: str, body: str, now: datetime) -> None:
        self.client.request("POST", f"/rest/api/3/issue/{ticket_id}/comment",
                            {"body": _adf(f"[{author}] {body}")})

    def set_status(self, ticket_id: str, status: TicketStatus, *, now: datetime) -> None:
        """Drive the issue to the target workflow status, walking intermediate statuses when
        there's no direct transition (e.g. Pending → Work in progress → Completed). Bounded
        to a few hops; if it can't get closer, it stops and the comment trail records reality."""
        target = _TO_JIRA_STATUS.get(status)
        if target is None:
            return
        target_rank = _STATUS_RANK.get(target.lower(), 99)
        for _ in range(4):   # bounded hops
            current = self._current_status(ticket_id)
            if current.lower() == target.lower():
                return
            transitions = self.client.request(
                "GET", f"/rest/api/3/issue/{ticket_id}/transitions").get("transitions", [])
            direct = next((t for t in transitions if t["to"]["name"].lower() == target.lower()), None)
            if direct is not None:
                self._do_transition(ticket_id, direct["id"])
                return
            # otherwise step forward: the smallest reachable status that advances toward the
            # target (rank strictly above current, not past target)
            cur_rank = _STATUS_RANK.get(current.lower(), 0)
            forward = [t for t in transitions
                       if cur_rank < _STATUS_RANK.get(t["to"]["name"].lower(), 99) <= target_rank]
            if not forward:
                return   # can't get closer — leave it; the comment trail records reality
            nxt = min(forward, key=lambda t: _STATUS_RANK.get(t["to"]["name"].lower(), 99))
            self._do_transition(ticket_id, nxt["id"])

    def _current_status(self, ticket_id: str) -> str:
        issue = self.client.request("GET", f"/rest/api/3/issue/{ticket_id}?fields=status")
        return ((issue.get("fields", {}).get("status") or {}).get("name") or "Open")

    def _do_transition(self, ticket_id: str, transition_id: str) -> None:
        self.client.request("POST", f"/rest/api/3/issue/{ticket_id}/transitions",
                            {"transition": {"id": transition_id}})

    def assign(self, ticket_id: str, assignee: str, *, now: datetime) -> None:
        # our model passes a human label; for escalation we assign the configured on-call
        # account. If none is configured, record the intended assignee as a comment instead.
        if self.oncall:
            self.client.request("PUT", f"/rest/api/3/issue/{ticket_id}/assignee",
                                {"accountId": self.oncall})
        else:
            self.add_comment(ticket_id, author="agent",
                             body=f"escalated — assign to {assignee}", now=now)

    def get(self, ticket_id: str) -> Ticket | None:
        try:
            issue = self.client.request(
                "GET", f"/rest/api/3/issue/{ticket_id}?fields=summary,status,labels,priority,"
                       "assignee,created,updated")
        except JiraError:
            return None
        comments = self.client.request(
            "GET", f"/rest/api/3/issue/{ticket_id}/comment").get("comments", [])
        return _to_ticket(issue, comments)

    def find_open_by_fingerprint(self, fingerprint: str) -> Ticket | None:
        issues = self._search(f'labels = "{_label(fingerprint)}" AND statusCategory != Done', 1)
        if not issues:
            return None
        return _to_ticket(issues[0], []).model_copy(update={"fingerprint": fingerprint})

    def list_open(self) -> list[Ticket]:
        return [_to_ticket(i, []) for i in self._search("statusCategory != Done", 50)]

    def _search(self, jql_tail: str, limit: int) -> list[dict]:
        jql = f"project = {self.project} AND {jql_tail} ORDER BY created DESC"
        resp = self.client.request("POST", "/rest/api/3/search", {
            "jql": jql, "maxResults": limit,
            "fields": ["summary", "status", "labels", "priority", "assignee", "created", "updated"]})
        return resp.get("issues", [])


# --- helpers ---------------------------------------------------------------------
def _label(fingerprint: str) -> str:
    return "sre-" + re.sub(r"[^a-z0-9]+", "-", fingerprint.lower()).strip("-")


def _adf(text: str) -> dict:
    """Minimal Atlassian Document Format wrapper — one paragraph per line."""
    paragraphs = [{"type": "paragraph",
                   "content": ([{"type": "text", "text": line}] if line else [])}
                  for line in text.split("\n")]
    return {"type": "doc", "version": 1, "content": paragraphs or [{"type": "paragraph"}]}


def _to_ticket(issue: dict, comments: list[dict]) -> Ticket:
    f = issue.get("fields", {})
    labels = f.get("labels", [])
    fingerprint = next((l for l in labels if l.startswith("sre-")), "unknown:unknown")
    status_name = ((f.get("status") or {}).get("name") or "Open").lower()
    assignee = (f.get("assignee") or {}).get("displayName")
    return Ticket(
        id=issue.get("key", "?"), fingerprint=fingerprint, title=f.get("summary", ""),
        services=[], fault_type="", severity=((f.get("priority") or {}).get("name") or "Medium"),
        status=_FROM_JIRA_STATUS.get(status_name, TicketStatus.OPEN), assignee=assignee,
        evidence="", created_at=_dt(f.get("created")), updated_at=_dt(f.get("updated")),
        comments=[Comment(ts=_dt(c.get("created")),
                          author=(c.get("author") or {}).get("displayName", "?"),
                          body=_adf_to_text(c.get("body"))) for c in comments])


def _dt(value: object) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _adf_to_text(body: object) -> str:
    """Flatten an ADF comment body back to plain text (best-effort)."""
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    out: list[str] = []

    def walk(node: dict) -> None:
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            walk(child)
        if node.get("type") in ("paragraph", "heading"):
            out.append("\n")   # keep block structure when flattening

    walk(body)
    return "".join(out).strip()


def _urllib_transport(method: str, url: str, headers: dict, body: str | None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body.encode() if body else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
