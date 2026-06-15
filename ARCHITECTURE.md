# ARCHITECTURE.md — system design

```
                 ┌────────────────────────────────────────────────┐
 lab log stream →│ 1. Ingestion │→ 2. Detection │→ 3. Incident Mgr │→ tickets (stub/JIRA)
 docker/health  →│   (tailer)   │   (no LLM)    │  (state machine) │→ notifications (stub/Teams)
                 └──────────────────────┬─────────────────────────┘
                                        ↓ on incident only
                          4. Diagnosis (LLM) → 5. Actions + HITL → verification
                                        ↓ on resolve
                          6. Post-mortems (stub/Confluence) ← also read as incident memory
                          9. Eval harness drives chaos injector and scores everything
```

## 0. Lab prerequisites (in logs-streaming-demo-app)

- Request ID injected at nginx, propagated webapp → api → worker, on every log line
- Every line tagged `service`, `timestamp`, `level`, `request_id`
- Loadgen logs response times and failures (the user-facing signal)
- Fault injector with labeled scenarios + ground-truth log (what, when)
- Change log recording deployments/config changes **and agent actions** with timestamps
- Maintenance-mode flag suppressing tickets/alerts during intentional tests

## 1. Ingestion layer

- Real-time tailer on the combined Docker log stream (streaming, not batch)
- Tolerant JSON parser; malformed lines are counted — a garbage spike is itself a signal
- Sliding-window store (5–15 min) for agent context
- Polled secondary signals: `docker ps` status/restart counts, per-service health
  endpoints, Redis queue depth, Postgres connection count

## 2. Detection layer (always-on, deterministic)

- Per-service error-rate thresholds vs. baseline (baseline learned from a healthy-traffic
  window; static is acceptable for the lab's stationary loadgen workload — see DECISIONS.md)
- Latency from loadgen (p95, not average)
- Silence detection (service stopped logging), crash-loop/restart detection,
  queue-depth growth
- Debounce: anomaly must persist ≥ 2 min before becoming a candidate
- Cooldown: one incident, one alert stream
- Change-log awareness: anomalies attributable to tagged agent actions are suppressed
- Output: `IncidentCandidate { services, signal_type, first_seen, evidence_sample }`

## 3. Incident Manager (the state machine)

Owns the incident lifecycle (states in AGENT.md) and is the **only** component allowed to
talk to the ticketing interface.

- **Correlation:** candidates within 90 s sharing an upstream dependency (topology map)
  merge into one incident attributed to the most upstream suspect
- **Fingerprinting:** `service:fault_type`, used for dedup, history lookup, reopen, flap
  detection
- **Dedup:** open ticket with same fingerprint → comment, never duplicate
- **Persistence:** incident state in SQLite; survives agent restart without re-acting
- Maintenance-mode check before any ticket/notification

## 4. Diagnosis layer (LLM, incident-triggered only)

Context assembler builds the prompt under a token budget, in strict priority order:

1. Request-ID correlated traces (the cross-service story)
2. Change log entries shortly before symptom onset
3. Filtered error-window logs for affected services
4. Neighbor-service logs
5. Topology map (always included; small)
6. Past post-mortems matching the fingerprint (incident memory)

The model may call read-only tools: `docker inspect`, health endpoints, queue length.
Output schema and acceptance rules are in AGENT.md (evidence refs verified, action must be
in catalog, no tier or confidence-based routing).

## 5. Action layer + HITL

- Enumerated action catalog; tier hard-coded per action (table in AGENT.md)
- Tier 1 executes immediately under guardrails; Tier 2 posts an approval request
  (15-min timeout → escalate); Tier 3 assigns to a human, agent does not act
- Approver and decision recorded on the ticket
- Verification: per-fingerprint recovery predicate over a 3-min window; fail → back to
  DIAGNOSING (max 2 loops) → ESCALATED

## 6. Ticketing (interface; stub first, JIRA later)

- Create on confirmed incident (post-debounce/correlation, not in maintenance, no open dup)
- Fields: service(s), fault type, severity, fingerprint, first-seen, evidence excerpt,
  log-window link
- Comment at every state transition; auto-resolve on verified recovery; ESCALATED stays
  open and assigned; reopen on fingerprint recurrence within the reopen window (with
  flap protection)

## 7. Post-mortems (interface; markdown first, Confluence later)

- Write: auto post-mortem per resolved incident — timeline from correlated logs, root
  cause, evidence, action, time-to-detect/diagnose/resolve, ticket link. Weekly health
  report: incident count, noisiest services, recurring fingerprints, agent accuracy
- Read: post-mortem archive feeds the diagnosis layer as incident memory

## 8. Notifications (interface; console first, Teams later)

- Incident card on creation (one-liner + severity + ticket link), Tier 2 approval
  requests, resolution notice with post-mortem link
- No raw log spam — the ticket is the record, chat is the doorbell

## 9. Evaluation harness

Built alongside layer 1, not after. For each labeled scenario, scores:

- Detected? Time-to-detect?
- Root cause correct vs. ground truth?
- Action appropriate; tier classification correct?
- Ticket hygiene: exactly one ticket per fault, correct lifecycle?

Standing tests: **null test** (1 hr healthy → total silence), **dedup test** (sustained
fault → one ticket, many comments), **HITL timeout test** (Tier 2 with no human → escalate,
no action), **correlation test** (multi-symptom single fault → one incident),
**flap test** (oscillating fault → one held-open ticket). MTTR dashboard derived from
ticket timestamps.

## 10. State & HA (the agent's own control plane)

The four stores (incidents, tickets, change log, post-mortems) plus the restart-cap ledger sit
behind interfaces. `state_backend` selects the substrate for all of them at once:

- **`sqlite`** (default) — per-host files in `.state/`; restart-safe on one host (D-009).
- **`postgres`** — a shared managed store so multiple replicas run against one source of truth
  (failure-safe, not just restart-safe). Same interfaces, same row↔model mappers (D-041).

**Coordination** is leader election (`--ha`): replicas contend for one Postgres advisory lock;
only the leader runs the incident lifecycle (correlate→ticket→diagnose→act→verify); standbys keep
tailing/polling so their window stays warm and take over on leader loss without re-acting (shared
state + idempotent actions). **Fleet guardrails** — the restart cap — are atomic across replicas
(`PostgresRateLimiter`, advisory-lock serialised). This DB is the agent's control plane, deliberately
separate from any monitored system's data of record (invariant #4). `deploy/k8s/agent-deployment.yaml`
runs 2 replicas with liveness/readiness probes for k8s self-healing.

## 11. Meta-monitoring (watching the watcher)

The agent is itself a monitored production service. It exposes a self-health HTTP server:

- **`/healthz`** — liveness, a dead-man's switch (fails if the tick loop hasn't run in ~6 ticks →
  k8s restarts a wedged agent).
- **`/readyz`** — readiness, leader-aware (a standby reports not-ready but stays warm).
- **`/metrics`** — Prometheus exposition of the self-SLO signals: detection latency, MTTR,
  escalation rate, action success, diagnosis latency, incident outcomes, `sre_last_tick`
  (heartbeat), `sre_leader`, `sre_up`.

`stream_blind` (D-013) is routed here as an agent-health **page**, never a lab ticket — the agent
going blind is its own incident. SLO alert rules (`deploy/alerts.yml`) page through the same
on-call the agent uses for lab incidents (dead-man's switch, agent-blind, no-leader) and ticket on
regressions (escalation rate, MTTR, a false-positive proxy). This layer is what makes the
shadow-mode rollout measurable — FP rate and accuracy are tracked, not asserted (invariant #6).
