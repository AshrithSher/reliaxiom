# CLAUDE.md — working in this repo

This repo contains the **SRE Agent**: an incident-detection/diagnosis/remediation agent
for the lab system at `C:\Mine\Concave\logs-streaming-demo-app`. Read [AGENT.md](AGENT.md)
and [ARCHITECTURE.md](ARCHITECTURE.md) before making design-level changes; record any new
design decision in [DECISIONS.md](DECISIONS.md).

## Non-negotiable invariants

1. **No LLM in the detection loop.** Detection is pure code: thresholds, debounce, cooldown.
   The LLM is called only after an Incident Candidate is confirmed.
2. **Action tiers are properties of actions, not LLM output.** The LLM selects an action
   from the enumerated catalog; the tier (auto / approval_required / escalate) is hard-coded
   per action in code. Never let model output set or override a tier.
3. **One fault = one ticket.** Candidates correlated via the topology map collapse into a
   single incident before any ticket is created. Dedup by fingerprint; comment, don't duplicate.
4. **Never touch postgres data.** No destructive DB actions in any tier, ever.
5. **The agent must not diagnose its own remediation.** Every action the agent takes is
   written to the change log; detection ignores anomalies caused by tagged agent actions.
6. **Silence on healthy systems.** False positives are treated as bugs of the highest
   severity. The null test (1 hour healthy → zero output) must always pass.

## Conventions

- Python is the implementation language (3.11+). Type hints everywhere; `pydantic` models
  for all cross-layer objects (IncidentCandidate, Incident, Diagnosis, ActionResult).
- All integrations (ticketing, notifications, post-mortems) go through interfaces defined
  in the integration layer. Local stubs (SQLite ticket store, markdown post-mortems,
  console notifications) are the default; real JIRA/Confluence/Teams are late-stage swaps.
- Incident state is persisted behind interfaces (`state_backend`): SQLite per-host by default,
  shared Postgres for HA (D-041). The agent must survive a restart — and, on the HA path, a whole
  replica dying — mid-incident without re-executing actions: actions check current reality before
  acting (idempotency), and only the elected leader acts.
- Every fault scenario added to the chaos injector gets a matching eval-harness scenario
  in the same PR. No detector or action ships without a scenario that exercises it.
- The agent is self-monitored (D-042): lifecycle changes emit Prometheus metrics on `/metrics` and
  the tick loop stamps a dead-man's-switch heartbeat. New lifecycle/escalation paths should record
  the matching metric; `stream_blind` is an agent-health page, never a lab ticket.
- New persisted state goes through a store interface with both a SQLite and a Postgres impl (reuse
  the row↔model mapper across both so they can't drift), exercised by the parametrized contract
  test in `tests/test_pg_stores.py`.
- Timestamps are UTC ISO-8601 everywhere. Fingerprints are `service:fault_type` strings.

## Testing

- Unit tests for detectors use recorded log fixtures, not the live stream.
- The eval harness (`eval/`) is the integration test: it runs labeled fault scenarios and
  scores detection time, root-cause accuracy, tier classification, and ticket hygiene.
- Run the null test before merging detector or threshold changes.

## What not to do

- Don't add raw log content to notifications — tickets are the record, chat is the doorbell.
- Don't create tickets while maintenance mode is on.
- Don't trust LLM confidence scores for routing; use the calibration signals in AGENT.md.
- Don't widen an LLM prompt's log window without checking the token-budget priority order
  (correlated traces > change log > error window > neighbor logs).
