"""The Incident — the agent's internal state object for one fault, distinct from the
external Ticket. Persisted so the agent can resume mid-incident after a restart."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from sre_agent.incident.lifecycle import IncidentState, validate_transition


class Incident(BaseModel):
    id: str
    fingerprint: str
    ticket_id: str | None
    state: IncidentState
    root_service: str
    services: list[str]
    fault_type: str
    severity: str
    first_seen: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    flap_count: int = 0
    remediation_loops: int = 0
    diagnosis_attempts: int = 0
    verify_started_at: datetime | None = None
    pending_action: str | None = None       # action awaiting human approval (Tier 2)
    pending_params: dict = Field(default_factory=dict)
    approval_deadline: datetime | None = None
    root_cause: str = ""                     # captured from diagnosis, for the post-mortem
    action_taken: str = ""

    def transition(self, dst: IncidentState, now: datetime) -> None:
        """Move to a new state, validating the edge. Stamps resolved_at on RESOLVED."""
        validate_transition(self.state, dst)
        self.state = dst
        self.updated_at = now
        if dst is IncidentState.RESOLVED:
            self.resolved_at = now
