# SRE Agent

An autonomous incident-response agent for the 7-container lab system in
`C:\Mine\Concave\logs-streaming-demo-app` (gateway → webapp → api → postgres/redis ← worker, plus loadgen as a synthetic user).

## Core principle

**Cheap code watches everything, always; the LLM wakes up only when something is wrong; humans approve anything risky.**

- Detection is deterministic (thresholds, debounce, cooldown) — no LLM in the hot loop.
- The LLM is invoked only to diagnose a confirmed incident, with assembled context.
- Actions come from an enumerated catalog; each action's risk tier is hard-coded, never chosen by the model.
- Risky actions require human approval (Teams); low-confidence cases escalate to a human untouched.

## Documents

| File | Purpose |
|------|---------|
| [CLAUDE.md](CLAUDE.md) | Instructions for Claude Code when working in this repo |
| [AGENT.md](AGENT.md) | Behavioral spec for the SRE agent itself — lifecycle, tiers, guardrails |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layer-by-layer system design |
| [DECISIONS.md](DECISIONS.md) | Design decisions and their rationale |
| [ROADMAP.md](ROADMAP.md) | Build order and milestones |

## Target system

The lab emits JSON logs (tagged `service`, `timestamp`, `level`, `request_id`) into one combined Docker log stream. A fault injector provides labeled scenarios with ground-truth timing, which the evaluation harness scores against.
