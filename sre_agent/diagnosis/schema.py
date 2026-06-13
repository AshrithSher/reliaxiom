"""The Diagnosis — structured output of the diagnosis layer. Note what is ABSENT: no tier
field and no routing confidence. The tier is resolved by code (action catalog); routing is
driven by the escalation guards, never by a model-reported confidence (invariants #2, #4)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from sre_agent.action.catalog import Tier


class Diagnosis(BaseModel):
    root_cause: str
    evidence: list[str] = Field(default_factory=list)
    selected_action: str
    action_params: dict = Field(default_factory=dict)
    alternatives: list[str] = Field(default_factory=list)
    tier: Tier                      # resolved in code, never from the model
    escalate: bool
    escalation_reasons: list[str] = Field(default_factory=list)
    # True when the LLM was unreachable (429/timeout) rather than the content being bad —
    # a transient condition the manager retries instead of permanently escalating
    provider_unavailable: bool = False
