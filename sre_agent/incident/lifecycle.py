"""Incident state machine. The lifecycle from AGENT.md, with transitions enforced in code
so an illegal jump is a loud error, never a silent corruption."""
from __future__ import annotations

from enum import Enum


class IncidentState(str, Enum):
    DETECTED = "DETECTED"
    DIAGNOSING = "DIAGNOSING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ACTING = "ACTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FLAPPING = "FLAPPING"


class InvalidTransition(Exception):
    pass


# Any active (non-terminal) state may escalate; that's folded in below.
_ALLOWED: dict[IncidentState, set[IncidentState]] = {
    IncidentState.DETECTED: {IncidentState.DIAGNOSING},
    IncidentState.DIAGNOSING: {IncidentState.AWAITING_APPROVAL, IncidentState.ACTING,
                               IncidentState.VERIFYING},
    IncidentState.AWAITING_APPROVAL: {IncidentState.ACTING},
    IncidentState.ACTING: {IncidentState.VERIFYING},
    IncidentState.VERIFYING: {IncidentState.RESOLVED, IncidentState.DIAGNOSING},
    IncidentState.RESOLVED: {IncidentState.FLAPPING},     # reopen only via flapping
    IncidentState.FLAPPING: {IncidentState.DIAGNOSING, IncidentState.RESOLVED},
    IncidentState.ESCALATED: set(),                       # terminal — a human owns it
}

# states from which escalation is always permitted
_CAN_ESCALATE = {
    IncidentState.DETECTED, IncidentState.DIAGNOSING, IncidentState.AWAITING_APPROVAL,
    IncidentState.ACTING, IncidentState.VERIFYING, IncidentState.FLAPPING,
}


def can_transition(src: IncidentState, dst: IncidentState) -> bool:
    if dst is IncidentState.ESCALATED and src in _CAN_ESCALATE:
        return True
    return dst in _ALLOWED.get(src, set())


def validate_transition(src: IncidentState, dst: IncidentState) -> None:
    if not can_transition(src, dst):
        raise InvalidTransition(f"{src.value} → {dst.value} is not a legal transition")
