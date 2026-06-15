# ROADMAP.md — build order and milestones

Each milestone is independently demoable and scored by the eval harness before moving on.

> **Part I (M0–M8) is DONE** — the autonomous agent, validated end-to-end on the lab
> (264 offline tests green; live auth/payments/db/redis/worker scenarios proven). It is the
> *proving ground*. **Part II (P0–P3, below)** turns that proven engine into the
> cloud-agnostic, MCP-native platform described in [VISION.md](VISION.md). Part II is ordered by
> **priority**, not just sequence: P0 items are blockers for running against any real system; P1
> earns trust at scale; P2 is the platform/ecosystem; P3 is continuous improvement.

---

# PART I — The agent (M0–M8) · DONE

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

---

# PART II — The platform (P0–P3) · PLANNED

Goal: the same engine, connected to **any application in any cloud or on-prem**, primarily
through **MCP** connectors (see [VISION.md](VISION.md)). The discipline of Part I carries over —
every phase ships with eval scenarios and stubs-before-real; **no invariant is relaxed**.
**Nothing below is implemented yet; this is the plan.**

Legend: each phase notes the SPI/interface it introduces and whether the seam already exists.

## ▸ P0 — Blockers (cannot run against a real system without these)

### P0.1 — Adapter SPI: make the four coupled seams swappable *(keystone)*
The engine already abstracts ticketing/notifications/post-mortems (D-006/D-024) and injects
poller IO (D-015). Extend the same discipline to the remaining environment-coupled seams:
- `TelemetrySource` — logs **and metrics and traces**, not just one log file. The
  `SlidingWindow` becomes a cache over a real backend; detectors query the SPI.
- `ActionBackend` — **DONE (D-037):** pluggable execution behind the `ActionBackend` SPI;
  `DockerActionBackend` (lab) + `KubernetesActionBackend` on a free local `kind` cluster with
  scoped RBAC, declarative idempotent rollout-restart, and API-confirmed verification.
- `TopologyProvider` — **DONE (D-039):** the dependency graph is read through the
  `TopologyProvider` SPI; `StaticTopologyProvider` wraps the lab literal (default) and
  `HttpTopologyProvider` fetches a `{service: [deps]}` adjacency doc (mesh/CMDB/Backstage),
  injected transport, degrading to the static fallback on any failure. Same `TopologyMap` API.
- `ChangeFeed` — deploys/config/flag events from real systems (today: local change log only).
- **Exit:** the lab runs unchanged through the new SPIs (stub impls) with all tests green —
  proving the seams don't perturb the engine.

### P0.2 — MCP client integration *(the universal connector)*
Make the agent an **MCP client** so a Ring-1 adapter can be backed by an MCP server with zero
bespoke code (VISION §3). Capability discovery enumerates a server's tools/resources on connect.
- **Action tools are mapped into the catalog with operator-assigned tiers — never auto-tiered;
  unmapped tools are inert** (invariant #2). Every MCP action is change-log-tagged before
  execution (invariant #5/#7).
- Telemetry MCP servers feed deterministic detectors only — **no LLM in detection** (#1).
- **Exit:** drive one real scenario end-to-end where telemetry comes from an MCP source and the
  remediation runs via an MCP `ActionBackend`, with the tier enforced in code.

### P0.3 — Control-plane security
Today anyone reaching the dashboard can start/stop the agent, inject faults, and **approve
Tier-2 actions** with no auth. Add: SSO/OIDC authn, RBAC on approvals + control endpoints, audit
log, TLS, CSRF. Approvals (and MCP-exposed approve tools) are the highest-value gate.
- **Exit:** an unauthenticated request cannot read incident detail, approve, or trigger control.

### P0.4 — Secrets + identity
Move LLM/observability/cloud/ITSM credentials out of `.secrets/*.env` into a secrets manager
(Vault / cloud secret managers) with workload identity; no long-lived tokens. Per-connector
least-privilege scoping. MCP servers are credentialed the same way.

### P0.5 — Global safety controls
A **kill switch** (halt all execution instantly) and **fleet-wide action rate limits**
(restart caps that hold across replicas, not per-process SQLite). First-class **shadow mode** as
a supported run posture (detect/diagnose/ticket, execute nothing) — gates every onboarding.
- **Rate limits — atomic single-host DONE (D-038):** the restart cap moved into an
  `ActionRateLimiter` SPI; the cap-count and the #5 tag are now one atomic transaction
  (`SqliteRateLimiter`, `BEGIN IMMEDIATE`), closing the check-then-act race for processes on one
  host. The multi-host swap (`guardrail_store='postgres'`) is wired behind the SPI and lands with
  HA (P1.2). Kill switch + first-class shadow-mode posture remain.

## ▸ P1 — Trust at scale (earns the right to auto-remediate real systems)

### P1.1 — Multi-signal correlation engine *(biggest design gap — D-014)* · **ENGINE DONE (D-040)**
Move beyond error-code-keyed merging to a correlation engine that fuses **metrics anomalies +
log spikes + trace error-rates + change events** on (topology proximity × adaptive time window ×
shared trace IDs × change coincidence). Promote request-trace correlation (already gathered in
`diagnosis/context.py`) into correlation itself. **Exit:** a multi-signal fault (latency,
mem-leak) collapses to exactly one incident, scored by primary signal.
- **DONE (D-040):** `Correlator` rewritten as an affinity graph (union-find → connected
  components) with edges across modalities — same-service, shared trace/request id (causal,
  window-bypassing), directional topology chain (D-016-safe), dep-error co-attribution — and an
  adaptive window. `trace_ids` promoted onto `Anomaly`/`IncidentCandidate` (populated by the
  error-rate detector); change-log coincidence feeds root attribution. Proven offline: the
  heterogeneous (`crash_loop`+`error_rate`) and cross-modal (log/metric/trace) twins of one fault
  collapse to a single incident/ticket. **Remaining:** populate `trace_ids` on the metric/trace
  detectors (extend the trace edge to the pure-metric path); add the live `latency`/`memleak` eval
  scenarios scored by primary signal (needs the lab up).

### P1.2 — HA + durable shared state · **DONE (D-041)**
Move incident/ticket/changelog/post-mortem stores behind their interfaces onto a managed
datastore (Postgres); run **multiple replicas** with leader election or fingerprint-sharded work
partitioning; fleet-wide guardrails live here. Removes the single-point-of-failure. **Exit:**
kill the leader mid-incident → a replica resumes without re-acting (idempotency, D-009, at fleet
scale).
- **DONE (D-041):** `state_backend=postgres` swaps all four stores onto shared Postgres behind
  the unchanged interfaces (`PostgresIncidentStore/TicketStore/PostMortemStore/ChangeLog`, reusing
  the SQLite row mappers). `PostgresRateLimiter` makes the restart cap fleet-atomic (advisory-lock
  serialised; 20-thread test holds at exactly `cap`). Coordination is leader election
  (`PostgresLeadership`, session-scoped advisory lock → crash failover with no lease timer); only
  the leader acts, standbys stay warm. `--ha`/`--postgres` CLI; `SingleNodeLeadership` is the no-op
  default. **Exit met live:** two replicas + live lab → one LEADER, one standby; killed the leader →
  standby took over (`/readyz` flipped), resuming on the shared state without re-acting.

### P1.2b — multi-host guardrail store (folded into P1.2)
`guardrail_store=postgres` is now realized by `PostgresRateLimiter` (D-041), closing the D-038
multi-host follow-up.

### P1.3 — Meta-monitoring + self-SLOs · **DONE (D-042)**
The agent emits its own Prometheus metrics (detection latency, **false-positive rate**, diagnosis
latency/cost, action success, MTTR, escalation rate), `/healthz` + `/readyz`, structured logs to
the central pipeline, and a **dead-man's-switch page** if it goes blind (`stream_blind`, D-013)
or its heartbeat stops. **This is what makes the shadow-mode rollout measurable.**
- **DONE (D-042):** dependency-free Prometheus exposition on `/metrics` (detection latency, MTTR,
  escalations, action success, diagnosis latency, incident outcomes, candidates, the
  `sre_last_tick` dead-man's-switch gauge, `sre_leader`/`sre_up`). `stream_blind` now routes as an
  agent-health page (not a lab ticket), closing a D-013 gap. SLO alerting shipped as code
  (`deploy/alerts.yml`): dead-man's-switch / agent-blind / no-leader pages + escalation/MTTR/FP-proxy
  tickets. The HA `deploy/k8s/agent-deployment.yaml` wires liveness=/healthz, readiness=/readyz, and
  Prometheus scrape. **Remaining:** diagnosis *cost* (tokens/$) needs provider usage; the FP alert is
  a proxy until labelled operator "not-an-incident" feedback exists (P3).

### P1.4 — Adaptive detection
Replace static baselines (honest demo simplification, D-010) with seasonal/adaptive baselining
and per-service config, so detection transfers to diurnal, deploy-shifted, real workloads.

## ▸ P2 — Platform & ecosystem (make it a product others extend)

### P2.1 — Agent-as-MCP-server
Expose read tools (query incidents, fetch post-mortems, subscribe to events) and **gated** write
tools (request/approve an action) so the agent is a node in an agent mesh and reachable from a
human's IDE/chat (VISION §3). Same approval path as dashboard/CLI; same tiers in code.

### P2.2 — Connector breadth + a connector catalog
Curated, tested adapters/MCP mappings for the common stacks (Prometheus/Grafana/Datadog/Loki/
CloudWatch/Elastic; Kubernetes/AWS/GCP/Azure/Terraform; PagerDuty/Opsgenie/Jira/ServiceNow/
Slack/Teams). Each ships with an eval scenario. Document the "write your own adapter" path.

### P2.3 — Policy-as-code + multi-tenancy
Per-team/per-environment action policies, tier overrides, maintenance/change-freeze calendars,
and blast-radius limits expressed as config — so one deployment serves many teams safely.

### P2.4 — Cloud-native packaging
Container image, Helm chart / operator, config via ConfigMaps/CRDs, autoscaling, CI/CD. The
*easy* last step — it rides on P0–P1 being real.

## ▸ P3 — Continuous improvement

### P3.1 — Real-incident replay eval
Replay recorded production incidents through the engine to score detection/diagnosis/tier
offline; regression-gate every change against history, not just synthetic chaos.

### P3.2 — Learned runbooks + analytics
FP-rate/MTTR trend analytics; mine the post-mortem archive to propose new catalog actions and
recovery predicates (still human-reviewed, tiers still in code) — the agent gets better as it
runs.
