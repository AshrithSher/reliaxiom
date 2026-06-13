"""The Diagnoser: prompt the LLM, parse its structured output, and apply the trust guards.

The model proposes a root cause + an action id from the catalog; code resolves the tier and
decides whether to escalate. Escalation triggers (calibration signals, not model confidence
— D-004): hallucinated evidence, disagreement across independent runs, an invalid/unknown
action, or malformed output.
"""
from __future__ import annotations

import json
from typing import Callable, Protocol

from sre_agent.action.catalog import Tier, resolve
from sre_agent.diagnosis.schema import Diagnosis

EvidenceVerifier = Callable[[str], bool]


class _Provider(Protocol):
    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str: ...


class Diagnoser:
    def __init__(self, provider: _Provider, runs: int = 2, temperature: float = 0.0) -> None:
        self.provider = provider
        self.runs = max(1, runs)
        self.temperature = temperature

    def diagnose(self, *, system: str, user: str,
                 verify_evidence: EvidenceVerifier,
                 restart_target: str | None = None) -> Diagnosis:
        parsed: list[dict | None] = []
        for _ in range(self.runs):
            try:
                text = self.provider.complete(system=system, user=user,
                                              temperature=self.temperature)
            except Exception as exc:  # provider/transport failure → transient, retryable
                return _escalation([f"diagnosis call failed: {exc}"], provider_unavailable=True)
            parsed.append(_parse(text))

        # Tolerate the model echoing the catalog's display form ("restart_container(service)")
        # as the id: strip a trailing parameter hint so a real, fixable action isn't rejected
        # as "unknown action" and needlessly escalated. Runs before the restart-target pin and
        # the resolve/agreement checks so they all see the bare id.
        for p in parsed:
            if p is not None and isinstance(p.get("selected_action"), str):
                p["selected_action"] = p["selected_action"].split("(", 1)[0].strip()

        # The service to restart is a deterministic fact — the correlated root_service —
        # not the model's to choose. Trusting the LLM's service param lets it silently
        # downgrade a Tier-2 stateful restart (postgres/redis) to a Tier-1 stateless one by
        # naming the symptom service it sees erroring. Pin restart_container's target to the
        # authoritative root before resolving the tier, so the tier follows from code (#2).
        if restart_target:
            for p in parsed:
                if p is not None and p.get("selected_action") == "restart_container":
                    params = p.get("action_params")
                    p["action_params"] = ({**params, "service": restart_target}
                                          if isinstance(params, dict)
                                          else {"service": restart_target})

        primary = parsed[0]
        if primary is None:
            return _escalation(["primary diagnosis was not valid JSON"])

        reasons: list[str] = []

        # disagreement across independent runs (on the actionable signature)
        if self.runs > 1:
            sigs = {_action_sig(p) for p in parsed}
            if None in sigs or len(sigs) > 1:
                reasons.append("independent diagnosis runs disagreed on the action")

        # hallucinated evidence
        bad = [e for e in primary["evidence"] if not verify_evidence(e)]
        if bad:
            reasons.append(f"cited evidence not found in logs: {bad[:3]}")

        # action validity + tier (resolved by code)
        resolved = resolve(primary["selected_action"], primary.get("action_params") or {})
        if not resolved.valid:
            reasons.append(f"action invalid: {resolved.reason}")

        escalate = bool(reasons)
        return Diagnosis(
            root_cause=primary["root_cause"],
            evidence=primary["evidence"],
            selected_action=primary["selected_action"],
            action_params=primary.get("action_params") or {},
            alternatives=primary.get("alternative_hypotheses") or [],
            tier=Tier.ESCALATE if escalate else resolved.tier,
            escalate=escalate,
            escalation_reasons=reasons,
        )


def _escalation(reasons: list[str], provider_unavailable: bool = False) -> Diagnosis:
    return Diagnosis(root_cause="undetermined", evidence=[], selected_action="none",
                     action_params={}, alternatives=[], tier=Tier.ESCALATE,
                     escalate=True, escalation_reasons=reasons,
                     provider_unavailable=provider_unavailable)


def _parse(text: str) -> dict | None:
    """Extract and parse the model's JSON object, tolerating ```fences and surrounding
    prose. Returns None if no valid object with the required keys is found."""
    candidate = _extract_json_object(text)
    if candidate is None:
        return None
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("root_cause"), str) \
            or not isinstance(data.get("selected_action"), str) \
            or not isinstance(data.get("evidence"), list):
        return None
    return data


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced {...} object in text, honoring JSON string quoting so a
    brace inside a string value doesn't end the scan. Tolerates markdown fences and any
    prose the model adds before or after the object."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _action_sig(parsed: dict | None) -> str | None:
    if parsed is None:
        return None
    params = parsed.get("action_params") or {}
    return parsed.get("selected_action", "") + "|" + json.dumps(params, sort_keys=True)
