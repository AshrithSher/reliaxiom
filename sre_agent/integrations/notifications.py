"""Notification integration boundary — 'chat is the doorbell, the ticket is the record'.
One-liners only, no raw log spam. Console/in-memory stubs now; Teams swaps in at M7."""
from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class Notification(BaseModel):
    kind: str            # "created" | "reopened" | "escalated" | "resolved"
    incident_id: str
    ticket_id: str | None
    severity: str
    title: str
    body: str


class Notifier(ABC):
    @abstractmethod
    def post(self, notification: Notification) -> None: ...


class InMemoryNotifier(Notifier):
    """Captures notifications for assertions in tests."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def post(self, notification: Notification) -> None:
        self.sent.append(notification)


class ConsoleNotifier(Notifier):
    def post(self, notification: Notification) -> None:
        ref = notification.ticket_id or notification.incident_id
        line = f"[{notification.severity}] {notification.kind.upper()} {ref}: {notification.title}"
        # for the kinds where the body carries the actionable detail (why escalated, what
        # needs approving), show it — the doorbell should say what it's ringing about
        if notification.kind in ("escalated", "approval", "reopened") and notification.body:
            line += f"\n    → {notification.body}"
        print(line, flush=True)
