# ROADMAP.md — build order and milestones

Each milestone is independently demoable and scored by the eval harness before moving on.

## M0 — Thin end-to-end slice (days, not weeks)
- Log tailer on the combined stream + tolerant JSON parser + sliding window store
- **Two** detectors with debounce + cooldown: error rate (paired with the `errors`
  chaos scenario) and silence (paired with `kill-worker`, which is the lab's *silent*
  fault — no errors anywhere, the worker just stops logging)
- Both fault scenarios wired to the lab's existing chaos toggles with ground truth
- Console output of IncidentCandidate objects
- **Eval harness skeleton**: run scenario, score detected/time-to-detect; null-test runner
- Exit criteria: both scenarios detected < 3 min; 1-hr null test silent

## M1 — Full detection + ingestion
- Remaining detectors: latency p95 (loadgen), silence, crash-loop/restart, queue growth,
  malformed-line spike
- Secondary signal pollers: docker ps, health endpoints, queue depth, pg connections
- Remaining fault scenarios: Redis OOM, Postgres lock, api 500s, added latency
- Exit criteria: every scenario detected; null test still silent

## M2 — Incident Manager
- State machine with SQLite persistence (restart-safe)
- Topology map + correlation (multi-symptom fault → one incident)
- Fingerprinting, dedup, reopen, FLAPPING state, maintenance-mode check
- Ticketing **stub** (SQLite store with JIRA-shaped semantics: states, comments, assignee)
- Exit criteria: correlation test (Redis OOM → exactly one ticket), dedup test, flap test

## M3 — Diagnosis layer
- Context assembler with token-budget priority order
- Request-ID trace correlation; change-log lookup
- LLM diagnosis with structured output; evidence-ref verification; double-run agreement
  check; read-only tools (docker inspect, health, queue length)
- Escalation triggers wired (unknown fingerprint, bad evidence, disagreement)
- Exit criteria: root cause correct on ≥ 4/5 scenarios vs. ground truth; unknown fault →
  clean Tier 3 escalation

## M4 — Tier 1 actions + verification
- Action catalog with hard-coded tiers; guardrails (restart caps, postgres protection,
  one action per pass); idempotency checks; dry-run mode
- Agent-action tagging in change log + detection suppression
- Per-fingerprint recovery predicates; verify → resolve / loop (max 2) → escalate
- Exit criteria: kill-worker auto-remediated end-to-end and ticket auto-resolved; agent
  does not alert on its own restart

## M5 — HITL (Tier 2/3)
- Approval flow on the notification **stub** (console prompt standing in for Teams):
  proposal with blast radius, approve/reject, decision recorded on ticket
- 15-min timeout → escalate, no action
- Exit criteria: HITL timeout test passes; Redis OOM happy path runs end-to-end with one
  approval

## M6 — Post-mortems + incident memory
- Markdown post-mortem generator (timeline, root cause, evidence, MTTx); weekly report
- Read path: fingerprint-matched post-mortems injected into diagnosis context
- Exit criteria: second occurrence of a known fault cites the prior post-mortem

## M7 — Real integrations
- Swap stubs for JIRA, Confluence, Teams behind the existing interfaces
- Exit criteria: full happy path (blueprint §lifecycle) against real services

## M8 — Hardening + full eval
- Complete scenario matrix scored: detection, diagnosis accuracy, tier correctness,
  ticket hygiene, MTTR dashboard from ticket timestamps
- Repeated null tests; chaos runs in maintenance mode produce zero tickets
