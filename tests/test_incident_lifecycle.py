"""TDD: the incident state machine. Transitions are validated in code — an illegal jump
raises, so a bug can't silently drive an incident into a nonsense state."""
import pytest

from sre_agent.incident.lifecycle import (
    IncidentState,
    InvalidTransition,
    validate_transition,
)


def test_happy_path_transitions_are_allowed():
    path = [
        IncidentState.DETECTED, IncidentState.DIAGNOSING, IncidentState.AWAITING_APPROVAL,
        IncidentState.ACTING, IncidentState.VERIFYING, IncidentState.RESOLVED,
    ]
    for a, b in zip(path, path[1:]):
        validate_transition(a, b)  # must not raise


def test_verifying_can_loop_back_to_diagnosing():
    validate_transition(IncidentState.VERIFYING, IncidentState.DIAGNOSING)


def test_any_active_state_can_escalate():
    for s in [IncidentState.DETECTED, IncidentState.DIAGNOSING,
              IncidentState.AWAITING_APPROVAL, IncidentState.ACTING, IncidentState.VERIFYING]:
        validate_transition(s, IncidentState.ESCALATED)


def test_resolved_reopens_only_via_flapping():
    validate_transition(IncidentState.RESOLVED, IncidentState.FLAPPING)
    with pytest.raises(InvalidTransition):
        validate_transition(IncidentState.RESOLVED, IncidentState.DIAGNOSING)


def test_escalated_is_terminal():
    with pytest.raises(InvalidTransition):
        validate_transition(IncidentState.ESCALATED, IncidentState.DIAGNOSING)


def test_illegal_jump_raises():
    with pytest.raises(InvalidTransition):
        validate_transition(IncidentState.DETECTED, IncidentState.RESOLVED)
