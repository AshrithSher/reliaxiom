# SRE Agent — Client Demo Deck (content & build guide)

> **How to use this file.** Every `## Slide N` block below is one slide. Each has three parts:
> **ON-SLIDE** (the words/visuals that go on the slide — keep them short), **SAY** (speaker notes —
> what you say out loud), and where useful **TECH** (the evaluation-grade detail a technical buyer
> will probe on). Non-technical folks follow the ON-SLIDE + SAY; technical folks read the TECH and
> the appendix. Nothing in here is aspirational unless it is explicitly under "Future scope" or
> "Planned" — every "today" claim is backed by working code and tests in this repo.
>
> **Honesty rule for this deck:** what is built is presented as built; what is planned is labeled
> planned. The product's entire value proposition is *trust*, so the deck must not oversell — an
> overclaim a technical evaluator catches kills the deal faster than a missing feature.

---

## Slide 1 — Title

**ON-SLIDE**
- **SRE Agent** — Autonomous incident detection, diagnosis & remediation
- *Cheap code watches everything. AI wakes only when something is wrong. Humans approve anything risky.*
- [Your company] · Client demo · June 2026

**SAY**
"What you'll see today is a working autonomous Site Reliability Engineer. It watches a running
system, catches faults in seconds, figures out the root cause with AI, and fixes the safe things by
itself — while asking a human before it touches anything risky, and never being able to do anything
destructive. It's not a slide-ware concept; it's running end-to-end on a 9-service application with
real metrics, logs, traces, Jira, Confluence, and Kubernetes, and it has 384 automated tests behind
it."

---

## Slide 2 — The one-line pitch (the whole product in one sentence)

**ON-SLIDE**
> "Same agent, three kinds of eyesight — it catches the fault on a real metric, shows you exactly
> where in the request it broke via the trace, fixes what's safe to fix itself, and asks a human
> before touching anything stateful."

**SAY**
"If you remember one sentence from today, this is it. Hold that thought — we'll prove every clause of
it live."

---

## Slide 3 — The problem (non-technical)

**ON-SLIDE**
- 2 a.m. page → a human squints at dashboards, greps logs, guesses, restarts something, waits.
- **MTTR** (mean time to recovery) is dominated by *human reaction + triage*, not the fix itself.
- Alert storms: one root fault → dozens of pages across services → on-call fatigue → real alerts missed.
- Tribal knowledge: "oh, that's just the worker, restart it" lives in three people's heads.

**SAY**
"Every operations team has the same story. Something breaks, a human gets paged, and most of the
downtime isn't the repair — it's the minutes or hours of a tired person figuring out *what* broke and
*where*. Worse, one underlying fault sets off a dozen alarms at once, so people get numb to alerts and
start ignoring them. And the actual know-how — what to do about each fault — lives in a few senior
engineers' heads. We're turning that judgment into software that runs 24/7."

---

## Slide 4 — Why this is hard / why naïve "AI ops" tools fail

**ON-SLIDE**
- **Naïve approach #1:** "Let an LLM watch the logs." → expensive, slow, flaky, hallucinates, and a
  confident-but-wrong model recommending a `DROP TABLE`-class action is a catastrophe.
- **Naïve approach #2:** "Static alerting + runbooks." → brittle, no root-cause reasoning, still pages a human for everything.
- **The trap:** false positives. An ops tool that cries wolf gets turned off in a week.
- **Our stance:** *false positives are treated as the highest-severity bug.* Silence on a healthy
  system is a hard guarantee, tested continuously.

**SAY**
"The obvious thing — point a large language model at your logs — is exactly the wrong architecture.
It's costly, it's slow, it's unreliable, and most dangerously, an AI that's 90% confident on a wrong
answer will happily tell you to do something destructive. The other extreme, classic threshold
alerting, just pages humans without thinking. We took a third path that gets the benefits of AI
reasoning without ever putting the AI in a position to do harm — and we obsess over not crying wolf."

---

## Slide 5 — The core principle (the architecture in one idea)

**ON-SLIDE**
> **Cheap deterministic code watches everything, always.
> The LLM is woken up only to diagnose a *confirmed* incident.
> Humans approve anything risky. The AI can never set its own permissions.**

Three layers of defense:
1. **Detection = pure code** (thresholds, debounce, cooldown). No LLM in the hot loop.
2. **Diagnosis = LLM**, but only after code confirms a real incident, and its output is fact-checked.
3. **Action = enumerated catalog**, with risk tiers **hard-coded in code** — never chosen by the model.

**SAY**
"Here's the key insight that makes the whole thing safe and cheap. Watching is done by simple,
fast, deterministic code — the kind of thing that can't hallucinate and costs nothing to run
constantly. The expensive, smart AI is only ever called *after* the cheap code has already confirmed
something is genuinely wrong. And when the AI proposes a fix, it can only pick from a fixed menu of
actions, and the *risk level* of each action is decided by us in code — the model has no say in
whether something is safe to auto-run. That separation is the entire safety story."

---

## Slide 6 — The 6 non-negotiable invariants (the trust contract) ⭐ technical

**ON-SLIDE** (these are enforced in code, not policy)
1. **No LLM in the detection loop** — detection is pure thresholds/debounce/cooldown.
2. **Action tiers are properties of actions, not LLM output** — the model picks an action *id*; code
   resolves the tier (auto / approval / escalate).
3. **One fault = one ticket** — correlated symptoms collapse into a single incident before any ticket.
4. **Never touch monitored-system data** — no destructive DB action in any tier, ever.
5. **The agent never diagnoses its own remediation** — every action is change-logged; detection
   ignores the transient the agent itself caused.
6. **Silence on healthy systems** — false positives are the worst-severity bug; the null test (1 hour
   healthy → zero output) must always pass.

**SAY**
"These six rules are the contract. They're not aspirations in a doc — they're enforced in the code
and guarded by tests. A technical evaluator should push on each one; we'll show you where each lives
in the code. Number two is the one that matters most: the AI proposes *what* to do, but whether that
action is safe enough to run automatically is a decision made by humans in code, never by the model."

**TECH** — invariant homes in code:
- #1 `sre_agent/detect/` (no provider import anywhere in the detection path)
- #2 `sre_agent/action/catalog.py::resolve()` returns the `Tier`; `Diagnosis` schema has *no* tier field
- #3 `sre_agent/incident/correlation.py` (affinity-graph union-find)
- #4 catalog has no destructive DB action; postgres restart is Tier-2 approval and touches the
  *container*, never data
- #5 `sre_agent/detect/engine.py::_suppressed()` consults the change log before emitting a candidate
- #6 `eval/harness.py --null`; `silence.py` collapses a total blackout to one `stream_blind` agent-health page

---

## Slide 7 — How it works: the pipeline (architecture) ⭐

**ON-SLIDE** (diagram — recreate this as boxes/arrows)
```
 log/metric/trace stream ─▶ 1. Ingestion ─▶ 2. Detection ─▶ 3. Incident Manager ─▶ Tickets (Jira)
   docker/health/queue        (tailer +        (NO LLM —        (state machine,        Notifications
                              telemetry SPI)   thresholds)      correlation, dedup)
                                                    │ on a CONFIRMED incident only
                                                    ▼
                              4. Diagnosis (LLM) ─▶ 5. Action + Human-in-the-loop ─▶ Verification
                                                    │ on resolve
                                                    ▼
                              6. Post-mortem (Confluence) ──▶ fed back as "incident memory"
                              9. Eval harness drives chaos + scores everything
```

**SAY**
"Left to right: we ingest signals, cheap code detects anomalies, the incident manager decides if it's
a real, single incident and opens *one* ticket. Only then does the AI diagnose. The action layer
either auto-fixes, asks a human, or escalates. Then it verifies the fix actually worked, writes a
post-mortem, and — this is neat — that post-mortem becomes memory the AI reads next time the same
fault happens. Everything is continuously scored by an evaluation harness so we know it's working."

**TECH** — each box is a Python package: `ingest/`, `telemetry/`, `detect/`, `incident/`,
`diagnosis/`, `action/`, `integrations/`, `eval/`. Cross-layer objects are all pydantic models
(`LogRecord`, `Anomaly`, `IncidentCandidate`, `Incident`, `Diagnosis`, `ActionResult`).

---

## Slide 7b — Why one fenced agent, not a "swarm of AI agents" ⭐ technical

**ON-SLIDE**
> **For an agent that takes actions on live infrastructure, auditability and zero false positives
> beat flexibility. The LLM reasons; deterministic code decides and acts.**

- **This is a single agent: one fixed pipeline, one tightly-scoped LLM call per incident** — *not* a
  mesh of autonomous agents negotiating with each other.
- **Multi-agent swarms earn their keep** on open-ended tasks with unknown steps (e.g. fan-out
  research). Incident response is the opposite: a **known, fixed pipeline** (detect → correlate →
  diagnose → act → verify). When the steps are known, encoding them in code beats re-discovering them
  with LLMs every time.
- **A swarm would fight our own safety contract:** more LLMs in more places = more nondeterminism,
  more hallucination surface, and control flow driven by model output — directly against invariants
  **#1** (no LLM in detection), **#2** (tiers are code), and **#6** (zero false positives).
- **Single-pipeline wins where it counts here:** auditable ("why did it page?" has one answer),
  cheaply evaluable (the eval harness scores deterministic behavior), and bounded in cost/latency
  (no N× token fan-out, no serialized agent round-trips inflating MTTR).
- **The HA replicas are not multi-agent** — they're identical copies of the *one* agent contending
  for a leader lock; only the leader acts. That's availability, not a swarm.

**SAY**
"You'll hear a lot of vendors pitch 'a team of AI agents.' We deliberately didn't build that, and for
this job it's the right call. A swarm makes sense when you don't know the steps in advance. But
incident response *is* a known sequence — watch, correlate, diagnose, fix, verify — so we encode that
sequence in reliable code and call the AI exactly once, for the one genuinely hard part: figuring out
the root cause. More AI agents would mean more places to hallucinate, control flow driven by model
guesses, and a system you can't audit or cost-bound — the opposite of what an ops team needs from
something that touches production. One disciplined agent that we can fully account for beats three
clever ones negotiating toward a confident wrong answer."

**TECH** — the pipeline stages are plain Python modules orchestrated by `incident/manager.py::step()`,
not autonomous agents; the only LLM entry point is `diagnosis/diagnoser.py` (double-run, fact-checked,
catalog-bounded — Slide 12). The diagnosis layer's read-only tool calls (`docker inspect`, health
endpoints, queue length) are single-agent tool use, not sub-agents. If diagnosis ever outgrows a
single context window, the bounded fallback is *sub-agents inside that one stage* (e.g. trace-analyst +
change-correlation-analyst → synthesizer), not replacing the pipeline — and only if evals show a
ceiling, never pre-built.

---

## Slide 8 — The system being watched (the lab) — what the demo runs against

**ON-SLIDE**
- A realistic **9-service microservice app**: `loadgen → gateway → webapp → api → {postgres, redis,
  auth}` and `worker → {redis, postgres, payments}`.
- Plus a full **free, self-hosted observability stack** (Grafana LGTM): Prometheus (metrics), Loki
  (logs), Tempo (traces), Grafana, cAdvisor, OTel collector — **16 containers total**.
- Every request carries a **request id** propagated across services → real distributed traces.
- A **chaos injector** can break any tier on demand with *labeled ground truth* (what broke, when).

**SAY**
"To prove the agent is real, we run it against a real-shaped system: nine services with a database, a
cache, an auth tier, a payments tier, background workers, and a synthetic user generating traffic. It
has production-grade observability — the same Prometheus/Grafana/Loki/Tempo stack you'd run yourself —
and it's all free and local. We can break any part of it on command, and because each fault is
labeled with ground truth, we can *score* whether the agent got it right, not just eyeball it."

**TECH** — topology lives in `sre_agent/incident/topology.py::LAB_TOPOLOGY`; `auth` and `payments`
are leaf deps (like postgres/redis). Faults are chaos toggles (dependency returns 5xx, fails fast)
rather than `docker stop` where possible, to produce clean single-signal cascades (D-027).

---

## Slide 9 — The incident lifecycle (state machine) ⭐ technical

**ON-SLIDE**
```
DETECTED → DIAGNOSING → [AWAITING_APPROVAL] → ACTING → VERIFYING → RESOLVED
               ↑                                            │
               └────────── re-diagnose (max 2 loops) ───────┘
                                 ↓
                             ESCALATED        FLAPPING ↔ (resolve/re-fire oscillation)
```
- **AWAITING_APPROVAL**: Tier-2 only; 15-min timeout → ESCALATED, **no action taken**.
- **VERIFYING**: per-fingerprint recovery predicate over a window; fail → re-diagnose; max 2 loops → ESCALATED.
- **FLAPPING**: re-fires within 30 min of resolve → reopen *once*, hold open; 3 cycles → escalate.
- **RESOLVED**: ticket auto-closed, post-mortem written.

**SAY**
"Every incident walks this lifecycle, and every transition is a comment on the ticket, so a human
always has a complete audit trail. Three things to notice: if a risky action needs approval and
nobody responds in 15 minutes, it escalates and does *nothing* — it never acts on a timeout. If a fix
doesn't actually work, it re-diagnoses, but only twice before handing off to a human — it knows when
to stop. And if a fault keeps flapping, it stops churning tickets and escalates."

**TECH** — `sre_agent/incident/manager.py` (`step()` orchestrates one cycle), `lifecycle.py`
(legal transitions), `models.py`. State persisted every transition (restart-safe, D-009).

---

## Slide 10 — Layer 1–2: Ingestion + Detection (cheap, deterministic, always-on)

**ON-SLIDE**
- **Ingestion:** real-time log tailer + tolerant JSON parser (malformed-line spike is itself a
  signal) + a 15-min sliding window. Secondary pollers: `docker ps`, health endpoints, Redis queue
  depth, Postgres connections — each degrades to "unknown" on failure, never crashes, never fabricates.
- **Detectors (all pure code):** error-rate, latency p95 (not average), silence, crash-loop,
  queue-growth, malformed-spike, container-down, plus metric/trace detectors (RED + USE).
- **Debounce** (must persist ≥2 min) + **grace** (one flaky tick allowed) + **cooldown** (one
  incident, one alert stream).

**SAY**
"The watching layer is deliberately boring — and that's the point. It's fast, it's cheap, it can't
hallucinate. It uses p95 latency, not averages, so a couple of slow requests don't hide behind many
fast ones and one outlier doesn't page you. An anomaly has to persist for two minutes before it
counts, which kills the vast majority of false alarms. And the secondary signals are treated as
context — if a poller can't reach something, it says 'I don't know' rather than making something up,
because a wrong reading is a lie the AI would later trust."

**TECH** — `detect/engine.py` is the only producer of `IncidentCandidate`; detectors read through the
`LogSource`/`MetricSource`/`TraceSource` SPIs (`telemetry/sources.py`) so a real backend (Loki/
Prometheus/Tempo) swaps in behind the in-memory window with zero detector changes (D-032). Thresholds
in `config.py` (e.g. `error_rate_threshold=10/60s`, `latency_p95_threshold_ms=1000`, `debounce_s=120`).

---

## Slide 11 — The marquee: correlation — "one fault = one ticket" ⭐⭐

**ON-SLIDE**
- One root fault throws *heterogeneous* symptoms across services (logs + metrics + traces + a deploy
  event). Naïve tools open a ticket per symptom → alert storm.
- We build an **affinity graph** over candidates and take its connected components — **one component =
  one incident**. Edges:
  - **E1** same service · **E2** shared trace/request id (causal — ignores the time window) · **E3**
    directional topology chain · **E4** dependency-error co-attribution.
- **Root attribution** precedence: dependency error code → recently-changed service (deploy
  coincidence) → most-upstream in topology → most-dependents.
- Example: Auth tier fails → api, webapp, gateway, loadgen *all* error → collapses to **one**
  `auth:unreachable` incident → agent restarts **auth (the root)**, not the symptoms.

**SAY**
"This is the smartest non-AI part of the system and the demo centerpiece. When auth breaks, four
other services light up with errors. A dumb tool pages you four times. Ours recognizes — using the
dependency graph, shared request IDs across the trace, and the error codes — that these are *one*
fault, opens *one* ticket, correctly blames *auth*, and fixes auth. We fixed the number-one failure
mode of naïve monitoring tools by design. And critically — two services merely sharing a dependency
do *not* get merged; it requires real causal evidence, so we don't over-collapse unrelated blips."

**TECH** — `incident/correlation.py`. Union-find connected components; E2 (shared trace id) bypasses
`correlation_max_span_s` because it's causal regardless of timing. Request ids are promoted from the
error-rate detector (`_distinct_request_ids`) — the same trace key diagnosis uses. D-016 preserved:
E3 requires a *directional* upstream/downstream relationship, not a shared leaf dependency (D-040).

---

## Slide 12 — Layer 4: Diagnosis (the LLM — fenced in on all sides) ⭐ technical

**ON-SLIDE**
- The LLM is called **only** on a confirmed incident, with a context prompt assembled under a token
  budget in strict priority order: **incident memory → correlated traces → change log → live signals
  → error window → neighbor logs → topology**.
- Structured JSON output: `root_cause`, `evidence[]`, `selected_action` (a catalog id),
  `action_params`, `alternative_hypotheses`. **No tier. No confidence used for routing.**
- **Anti-hallucination guards (escalate, don't trust):**
  - Cited evidence must resolve to *real* tokens in the prompt (request ids / error codes) — fabricated refs → escalate.
  - **Two independent runs** must agree on the action — disagreement → escalate.
  - Action must be in the catalog with valid params — else escalate.
  - The restart *target* is pinned to the **correlated root**, not the model's pick (stops the model
    silently downgrading a Tier-2 stateful restart to Tier-1).
- Transient provider failure (429/timeout) → **retry**, not escalate (self-heals when the API returns).

**SAY**
"Now the AI. Notice everything around it. We feed it a carefully prioritized context — including
memory of how we fixed this exact fault before. It returns structured JSON, and then we *fact-check
it*: every piece of evidence it cites has to actually exist in the logs we gave it, or we don't trust
it. We run it twice and require agreement. It can only name an action from our fixed menu. And it
does *not* get to choose which service to restart — we already know the culprit from correlation, so
a confident-but-wrong model can't redirect a risky restart to the wrong place. We deliberately do
*not* use the model's confidence score for anything — models report 90% confidence on plausible wrong
answers."

**TECH** — `diagnosis/diagnoser.py` (guards + double-run), `diagnosis/context.py` (token-budget
assembler, `make_verifier` token-overlap check), `diagnosis/schema.py` (no tier field),
`diagnosis/llm.py`. Provider is swappable behind `LLMProvider.complete()`: **OpenRouter primary +
Gemini fallback** today (`FallbackProvider` so a free-tier 429 can't kill a live demo); an Anthropic
Claude provider is a one-class swap. `diagnosis_runs=2`, `diagnosis_budget_chars=6000`.

---

## Slide 13 — Layer 5: The action catalog + tiers (the safety model) ⭐⭐

**ON-SLIDE**

| Action | Tier | Behavior |
|---|---|---|
| `restart_container` (stateless: api, webapp, worker, gateway, auth, payments, loadgen) | **1 — AUTO** | agent fixes it itself |
| `clear_stuck_queue_item`, `rerun_failed_job` | **1 — AUTO** | idempotent, single-item |
| `restart_container` (stateful: **redis, postgres**) | **2 — APPROVAL** | waits for a human |
| `flush_queue`, `change_config` (whitelisted keys) | **2 — APPROVAL** | possible data loss / risk |
| anything else / unknown / invalid | **3 — ESCALATE** | hand to a human, agent does not act |

- **The tier is resolved in code from the action + target**, never from the model.
- **Guardrails (code-enforced, all tiers):** max 3 restarts/service/hour → escalate; one action per
  pass; **never any destructive postgres-data action**; idempotency check against live state.

**SAY**
"This table *is* the product's safety guarantee. Restarting a stateless service is safe, so the agent
does it automatically. Touching the database or cache is risky, so it stops and asks a human — with a
blast-radius summary so the approver knows what's affected. Anything it doesn't recognize goes to a
human untouched. And there is *no* action, in any tier, that can destroy your data — that's not a
setting, it's that the capability doesn't exist in the code. There's also a restart cap: if it's
restarted the same service three times in an hour, it stops trying and escalates — it knows when it's
not helping."

**TECH** — `action/catalog.py::resolve()` (the tier authority), `action/executor.py` (dry-run gate +
capability gate + delegate), `action/ratelimit.py` (the cap is enforced **atomically** with the
change-log tag in one transaction, so it holds across replicas — D-038). Execution is behind the
`ActionBackend` SPI: `DockerActionBackend` (lab) or `KubernetesActionBackend` (idempotent rollout-
restart via the k8s API under least-privilege RBAC — D-037).

---

## Slide 14 — Layer 5b: Human-in-the-loop + verification

**ON-SLIDE**
- **Approval (Tier 2):** the agent posts a proposal with the **blast radius** (target + topology
  dependents), persists the pending action (survives a restart), and waits. Approve in the UI → it
  acts and records *who* approved on the ticket. Reject → escalate. Timeout (15 min) → escalate, no action.
- **Verification is a per-fingerprint *predicate*, not "looks normal":** e.g. a latency fault is only
  "recovered" when the **real Prometheus p95** is back under threshold; an unreachable dependency only
  when the **container is actually back up**; a queue fault only when **depth is below threshold and draining**.
- Verify on the **same signal you detected on** — so a still-slow service is never marked healthy.

**SAY**
"For risky actions, the agent behaves like a careful junior engineer: it writes up exactly what it
wants to do and what it'll affect, and waits for a senior to click approve — and your name goes on
the record. If you don't respond, it escalates rather than guessing. And it doesn't declare victory
just because things 'look quiet' — recovery is a specific, measurable condition per fault type,
checked against the same metric that detected the problem. A database that's still down can't be
faked into 'resolved.'"

**TECH** — `incident/manager.py` (`request_approval`/`approve`/`reject`/`check_approval_timeouts`),
`action/recovery.py` (predicates; metric checks degrade to log checks if the backend is unreachable —
never wedges an incident open, never declares premature victory; D-035). `approval_timeout_s=900`,
`recovery_window_s=180`, `max_remediation_loops=2`.

---

## Slide 15 — Self-healing, idempotency & "never chase your own tail"

**ON-SLIDE**
- Every agent action is written to a **change log tagged `actor: sre-agent` before execution** →
  detection ignores the anomaly the agent's own restart causes (invariant #5).
- Actions are **idempotent against live reality** — the agent checks current state before acting, so
  a crash mid-action never double-restarts or double-flushes.
- Incident state is **persisted**; the agent survives a restart mid-incident and resumes without re-acting.

**SAY**
"Two subtle but critical behaviors. First, when the agent restarts something, that restart looks like
a blip — so without care, the agent would 'detect' its own fix as a new fault and loop forever. We
prevent that by logging every action before it happens and teaching detection to ignore it. Second,
if the agent itself crashes in the middle of handling an incident, it picks up exactly where it left
off when it restarts — and because actions check reality first, it never does the same thing twice."

**TECH** — `changelog.py`, `detect/engine.py::_suppressed()`, `action/backend.py::observe()`
(live-state read before `apply()`), `incident/store.py` (SQLite, save-on-transition).

---

## Slide 16 — High availability: no single point of failure ⭐ technical

**ON-SLIDE**
- Default: per-host SQLite — **restart-safe**.
- HA path: **all** control-plane state (incidents, tickets, change log, post-mortems, restart-cap
  ledger, leader lock) moves to **shared Postgres** behind the *same interfaces* — **failure-safe**.
- **Leader election:** multiple replicas; only the **leader** acts; standbys stay warm (keep
  tailing/detecting). Leader dies → a standby takes over **mid-incident without re-acting**.
- Uses a session-scoped Postgres advisory lock → crash failover with **no lease timer to tune**.
- **The agent's state DB is deliberately separate from the monitored system's data** (invariant #4).

**SAY**
"An SRE tool being down during an incident is the worst possible moment, so the agent is built to be
as reliable as the systems it watches. You can run multiple copies; they elect one leader to act
while the others stand by warm. Kill the leader mid-incident and a standby resumes on the shared
state without redoing any action. This is proven live — we kill the leader and watch failover happen."

**TECH** — `ha/leader.py` (`PostgresLeadership` session-scoped `pg_try_advisory_lock`;
`SingleNodeLeadership` no-op default), `state_factory.py`, `pg_store.py`, `pg_ticketing.py`,
`pg_postmortems.py`, `pg_changelog.py`, `pg_ratelimit.py` (20-thread test proves the cap holds at
exactly the limit). `deploy/k8s/agent-deployment.yaml` runs 2 replicas with liveness/readiness
probes (D-041).

---

## Slide 17 — Watching the watcher: self-monitoring & self-SLOs

**ON-SLIDE**
- The agent is itself a monitored production service. It serves:
  - **`/healthz`** — liveness with a **dead-man's switch** (fails if the tick loop stalls → k8s restarts it).
  - **`/readyz`** — readiness, leader-aware.
  - **`/metrics`** — Prometheus: detection latency, **MTTR**, escalation rate, action success,
    diagnosis latency, incident outcomes, a heartbeat gauge, leader status.
- **`stream_blind`** (the whole stream goes quiet → the agent is blind) is routed as an **agent-health
  page**, never a fake "everything is down" alert storm.
- SLO alert rules ship as code (`deploy/alerts.yml`): dead-man's-switch / agent-blind / no-leader
  pages + escalation/MTTR/false-positive-proxy tickets.

**SAY**
"You can't trust a watchdog that can silently go to sleep. So the agent emits its own health metrics
and has a dead-man's switch — if its main loop stops ticking, Kubernetes restarts it automatically.
If it goes blind because the whole log stream dried up, it raises *one* 'I can't see' page instead of
pretending every service died. This is also what makes a safe rollout measurable — we can actually
track the false-positive rate and MTTR over time, not just assert them."

**TECH** — `health.py`, `metrics.py` (dependency-free Prometheus exposition — no `prometheus_client`
needed), manager lifecycle hooks (`AgentMetrics`), `NullMetrics` no-op when off. D-042.

---

## Slide 18 — Integrations (it plugs into what you already use)

**ON-SLIDE**

| Integration | What it is | Default |
|---|---|---|
| **Ticketing** | **Jira** (project, ADF comments, workflow transitions, severity→priority, fingerprint dedup label) | on (local SQLite mirror always kept) |
| **Post-mortems** | **Confluence** pages (timeline, root cause, evidence, MTTx) | on (local markdown mirror always kept) |
| **Metrics / Logs / Traces** | Prometheus / Loki / Tempo (Grafana LGTM) | swappable SPI |
| **Remediation** | **Docker** (lab) or **Kubernetes** (scoped RBAC) | swappable SPI |
| **LLM** | OpenRouter (primary) + Gemini (fallback); Anthropic = one-class swap | swappable interface |
| **Notifications** | console now; Teams/Slack = interface swap | stub |

- Every integration is behind an interface; **real services degrade to a local mirror** if
  unreachable — an Atlassian outage never blocks the agent.

**SAY**
"It's not a walled garden. Incidents become real Jira tickets with the full lifecycle reflected in
the Jira workflow; post-mortems publish to Confluence. It reads your existing Prometheus/Loki/Tempo.
It can remediate on Docker or on real Kubernetes. And every one of these is behind a clean interface,
so if Jira is down, the agent keeps a local copy and keeps working — and swapping Teams or ServiceNow
in later is mechanical, not a rewrite."

**TECH** — `integrations/jira.py`, `confluence.py`, `postmortems.py` (Composite stores tee to
real + local), `telemetry/adapters.py`, `action/k8s_backend.py`. Credentials in gitignored
`.secrets/*.env` (D-024, D-033, D-037).

---

## Slide 19 — Kubernetes remediation under a locked-down identity (enterprise headline) ⭐ technical

**ON-SLIDE**
- The agent restarts services on real Kubernetes via the API — as a ServiceAccount with a Role
  granting **exactly** `get / list / patch` on Deployments in **one** namespace. Nothing else.
- Provable, by asking Kubernetes itself:
  - `can-i patch deployments` → **yes**
  - `can-i delete deployments` → **no**
  - `can-i get secrets` → **no**
  - `can-i '*' '*'` → **no**
- Restart = a declarative, **idempotent** rollout-restart; recovery verified against the Deployment's
  `readyReplicas` / `observedGeneration`, not a command exit code.

**SAY**
"For the security team, this is the headline. On Kubernetes the agent runs as an identity that is
boxed into exactly one safe verb on one resource type in one namespace. You can ask Kubernetes
directly what the agent is allowed to do, and the answer is: it can restart a deployment, and
literally nothing else — it can't delete anything, can't read your secrets, can't escalate. The blast
radius is an explicit, auditable permission grant, not 'trust the agent.'"

**TECH** — `deploy/k8s/rbac.yaml`, `action/k8s_backend.py` (injected transport, canned-JSON unit
tests + live `kind` validation). Strategic-merge PATCH = the `rollout restart` mechanism. D-037.

---

## Slide 20 — LIVE DEMO script (the part that sells it)

**ON-SLIDE** (run these from the dashboard at http://localhost:8000 — click, don't type)

| # | Inject | What the audience sees | Agent response | Tier |
|---|---|---|---|---|
| 1 | **API error spike** | 5xx ratio for api/webapp climbs (Grafana panel) | restart api, verify 5xx dropped | Auto ✅ |
| 2 | **API latency** | real p95 jumps ~50ms → thousands | restart api, verify p95 back under threshold | Auto ✅ |
| 3 | **Worker killed** (silent fault) | queue_depth climbs, no errors anywhere | restart worker, verify it logs + queue drains | Auto ✅ |
| 4 | **Auth tier failing** (cascade) | 5xx on auth **and** api/webapp at once | collapse to **one** `auth:unreachable`, restart auth (root) | Auto ✅ |
| 5 | **Payments failing** | worker errors charging, queue grows | one `payments:unreachable`, restart payments | Auto ✅ |
| 6 | **Postgres down** | db_unreachable cascade | roots at postgres (stateful) → **AWAITING_APPROVAL** → you approve in UI | **Approval 🖐** |
| 7 | **Redis down** | cache_degraded → redis_unreachable | roots at redis (stateful) → **approval** | **Approval 🖐** |
| 8 | **Restart-cap escalation** | trigger errors, let it fix 3× in an hour | cap hit → **ESCALATED** ("knows when to stop") | **Escalate ⚠** |

**For each fault, point at four things updating live:** (1) topology nodes turn red → green; (2)
incident walks `detecting → diagnosing → acting → verifying → resolved`; (3) incident drawer shows the
**LLM root-cause text + evidence + a real Jira link**; (4) KPI strip: auto-resolve %, MTTR, pages avoided.

**SAY (demo narration arc)**
"Faults 1–3: it fixes them itself, end to end, and shows you its reasoning and a real Jira ticket.
Fault 4 is the money shot — watch four services light up and collapse into one incident blamed
correctly on auth. Faults 6 and 7 show the brakes: it correctly refuses to auto-touch the database or
cache and waits for your approval — you click approve and it finishes. Fault 8 shows judgment: after
three restarts it stops and escalates instead of flailing. That's the full range — autonomy where
safe, deference where risky, restraint when it's not working."

**TECH / reset note** — reset between faults (dashboard *Reset lab*) or a new fault dedups onto the
prior incident. Demo config uses a 2/hr restart cap so the escalation is quick to show. Full per-fault
PromQL/TraceQL is in `DEMO.md`.

---

## Slide 21 — The null test (the most important slide for skeptics)

**ON-SLIDE**
- **1 hour of a perfectly healthy system → ZERO incidents, ZERO tickets, ZERO pages.**
- Run automatically: `python -m eval.harness --null`.
- False positives are treated as the **highest-severity bug** in the project.

**SAY**
"Anyone who's run an ops tool knows the real test isn't 'can it catch a fault' — it's 'does it shut
up when nothing is wrong.' We run the agent against a healthy system for an hour and it must produce
absolutely nothing. If it ever pages on a healthy system, that's a top-priority bug for us, not a
tuning preference. This is why people will actually leave it turned on."

---

## Slide 22 — Proof & validation (genuine numbers, no rounding up) ⭐

**ON-SLIDE**
- **394 automated tests** — 384 pass offline with zero external dependencies; 10 are skipped unless a
  live Kubernetes cluster / Postgres is wired (they pass there).
- **Validated live, end-to-end** on the 9-service stack:
  - auth-down detected in **~130s**, payments-down in **~140s**, metric error cascade in **~143s**.
  - Example resolved incident MTTR: **105s** (detect → diagnose → restart → verify, fully autonomous).
  - HA failover proven: killed the leader → standby took over (`/readyz` flipped 503→200), resumed
    without re-acting.
  - Restart cap holds at exactly the limit under 20 concurrent threads.
- **Every fault scenario in the chaos injector has a matching scored eval scenario** — no detector or
  action ships without a test that exercises it.

**SAY**
"Let me be precise, because precision is the point. There are 394 tests; 384 run anywhere with no
setup, and the other 10 need a real cluster or database and pass there. Detection times on real
faults are in the two-minute range — that's deliberately *after* a debounce window that kills false
alarms, not a reaction-speed limit. We've watched it take a fault from detection to verified fix in
under two minutes with no human involved. And we've killed the leader process mid-incident and
watched a standby finish the job."

**TECH** — `eval/harness.py` (detection scoring + null test), `eval/diagnosis_eval.py` (root-cause
accuracy vs ground truth, ~4/5), `tests/` (unit + integration). Numbers from DECISIONS.md D-027/
D-033/D-041 live-validation notes and `.state/postmortems/`.

---

## Slide 23 — From lab to your stack: the platform vision (3 rings + MCP) ⭐ technical

**ON-SLIDE**
```
RING 0 — THE ENGINE (substrate-agnostic, BUILT): detect · correlate · state machine ·
         diagnose · tier · guardrails · verify · HITL · post-mortem · incident memory
              ▲ Provider SPIs (interfaces)
RING 1 — ADAPTERS (per-environment, swappable): TelemetrySource ✅ · ActionBackend ✅ ·
         TopologyProvider ✅ · ChangeFeed (planned) · TicketStore ✅ · Notifier · PostMortemStore ✅
              ▲ mostly spoken over
RING 2 — CONNECTORS (the wire): MCP servers (primary) + native SDK/REST adapters
```
- **The thesis:** the engine never learns what cloud you run on — the adapters do, and most adapters
  are just **MCP servers** (Prometheus, Loki, Datadog, Kubernetes, AWS/GCP/Azure, Jira, PagerDuty…).
- **Onboarding discipline:** **Shadow** (detect/diagnose/ticket, execute nothing) → **Suggest** (every
  action is a Tier-2 proposal) → **Auto** (whitelist proven low-risk fingerprints). A global kill
  switch + fleet rate limits gate auto.

**SAY**
"What you saw runs on a lab, but the engine itself is environment-agnostic by design — all the
intelligence operates on abstract objects, and everything specific to *your* environment is a
pluggable adapter. The strategy is to connect to your stack primarily through MCP, the emerging open
standard that observability and cloud vendors are already adopting. And nobody flips it to full
autonomy on day one: you run it in shadow mode for a week, watch its false-positive rate and accuracy
on *your* real incidents, then graduate one safe fault type at a time. Trust is earned and measured,
not assumed."

**TECH** — three of four Ring-1 read seams are SPIs *today* (`TelemetrySource` D-032, `ActionBackend`
D-037, `TopologyProvider` D-039); ticketing/post-mortems were already interfaces (D-006/D-024).
`ChangeFeed` and the Ring-2 MCP client/server are the next planned work (VISION.md, ROADMAP P0.2/P2.1).

---

## Slide 24 — What is real today vs. planned (the integrity slide) ⭐

**ON-SLIDE**

**BUILT & VALIDATED (Part I, M0–M8 + platform P0.1/P1.1/P1.2/P1.3):**
- Full detect → correlate → diagnose → act → verify → post-mortem loop, autonomous, end-to-end
- Multi-signal affinity-graph correlation; metric/log/trace detection; per-fingerprint recovery
- Action catalog + hard-coded tiers + guardrails + restart cap; HITL approval; escalation
- Real Jira + Confluence; Prometheus/Loki/Tempo; Docker **and** Kubernetes remediation
- HA (Postgres shared state + leader election + failover); self-monitoring (/healthz /readyz /metrics)
- Swappable SPIs for telemetry, actions, topology; 384 passing tests + eval harness + null test

**PLANNED (Part II — labeled, not hidden):**
- `ChangeFeed` SPI (real deploy/flag events) · **MCP client + agent-as-MCP-server**
- Control-plane security (SSO/OIDC, RBAC on approvals, audit log, TLS) · secrets manager + workload identity
- Global kill switch + first-class shadow-mode posture · adaptive/seasonal baselines · connector catalog
- Helm chart/operator packaging · real-incident replay eval · learned-runbook analytics

**SAY**
"I want to be completely straight about what's done and what's next, because in this category an
overclaim you catch later would cost us your trust — which is the whole product. Everything in the top
box works today and is tested. The bottom box is the roadmap, and it's genuinely roadmap, not vapor.
The biggest near-term items are the MCP connectivity that makes onboarding zero-code, and the
control-plane security hardening — today the dashboard's approve button has no auth, which is fine for
a demo and explicitly first on the list before any real deployment."

---

## Slide 25 — Honest current limitations (pre-empt the tough questions)

**ON-SLIDE**
- **Static detection baselines** — fine for steady lab traffic; real diurnal/deploy-shifted workloads
  need adaptive baselining (planned, P1.4).
- **Dashboard control plane is unauthenticated today** — anyone reaching it can approve/inject.
  SSO/RBAC/audit is P0.3, gated before production.
- **Diagnosis cost (tokens/$) not yet tracked** — latency is; cost needs provider usage reporting.
- **False-positive metric is a proxy** today (incidents without a successful action) until labeled
  operator "not a real incident" feedback exists.
- **One agent per host on the SQLite path** — multi-replica requires the Postgres/HA path.
- Demo resets between faults (overlapping un-reset faults can produce extra incidents — a known
  correlation edge).

**SAY**
"Here are the real limitations, on the record. The detection thresholds are static — great for a
stable system, but a real production workload that's busy at noon and quiet at 3 a.m. needs adaptive
baselines, which is on the roadmap. The demo dashboard has no login yet. And our false-positive
number is currently a proxy until we wire in human 'that wasn't real' feedback. I'd rather you hear
these from me than find them yourself."

---

## Slide 26 — Business value / ROI (non-technical close)

**ON-SLIDE**
- **Faster recovery:** detection in ~seconds-to-minutes + autonomous fix → MTTR collapses for the
  common, well-understood faults (the majority of pages).
- **Fewer pages:** auto-resolved incidents are **pages avoided** — measured on the dashboard KPI strip.
- **No alert storms:** one fault = one ticket → on-call fatigue drops, real signals don't get buried.
- **Captured tribal knowledge:** post-mortems become reusable memory; the agent gets *better* as it
  sees a fault repeat.
- **24/7 senior-engineer judgment** at software cost — and safely fenced so it can't make things worse.
- **Auditable:** every decision, action, and approval is on the ticket + change log.

**SAY**
"The business case is simple. Most production pages are the same handful of well-understood faults
that a senior engineer would fix in a few minutes — if they were awake and watching. This agent
handles those automatically, around the clock, and only wakes a human for the genuinely novel or
risky cases. You recover faster, you page people less, you stop drowning in duplicate alerts, and the
know-how stops walking out the door — it gets written down and reused. And every bit of it is
auditable, which your compliance team will care about."

---

## Slide 27 — Why us / differentiation

**ON-SLIDE**
- **Safety-first architecture, not AI-first** — the LLM is fenced on all sides; tiers are code; data
  is untouchable. Competitors that "let the AI act" can't make this guarantee.
- **One fault = one ticket** correlation across logs+metrics+traces+deploys — solves the #1 pain of
  legacy alerting out of the box.
- **Earns trust measurably** — shadow → suggest → auto, with a real FP/MTTR dashboard.
- **Substrate-agnostic engine + MCP** — connect to any stack without forking the product.
- **Production-grade from day one** — HA, self-monitoring, scoped RBAC, 384 tests, an eval harness.

**SAY**
"Plenty of vendors are slapping an LLM on logs. The difference is our entire architecture is built so
the AI *can't* hurt you, and so you can prove that to your security team. We solve the alert-storm
problem by design, we earn autonomy gradually with measured evidence, and the engine is built to plug
into whatever you already run. This isn't a prototype — it has the HA, self-monitoring, and test
discipline of a real product."

---

## Slide 28 — Call to action

**ON-SLIDE**
- **Proposed next step:** a **shadow-mode pilot** on one of your services — detect/diagnose/ticket,
  execute nothing — for ~1–2 weeks.
- We report back: false-positive rate, detection times, root-cause accuracy on *your* real incidents.
- Then graduate one safe fault type to auto-remediation, on your timeline.
- **What we'd need from you:** read access to your telemetry (Prometheus/Loki/Tempo or equivalents),
  a Jira project, and a list of services to watch. No write access until you say so.

**SAY**
"Here's what I'd propose. Let us run it in shadow mode against one of your services — it watches,
diagnoses, and files tickets, but doesn't touch anything. After a couple of weeks we sit down with
*your* numbers: how often it was right, how fast it caught things, how many false alarms. If those
numbers earn it, we turn on automatic fixing for one safe, common fault — and expand from there at
whatever pace you're comfortable with. To start, we only need read access to your monitoring and a
Jira project. We don't get write access to anything until you decide we've earned it."

---

# APPENDIX (for the technical evaluation / leave-behind)

## A1 — Config knobs that matter (defaults, all override via `--config` JSON)

| Knob | Default | Meaning |
|---|---|---|
| `debounce_s` | 120 | anomaly must persist this long → candidate (kills false positives) |
| `grace_s` | 20 | one flaky tick allowed without resetting debounce |
| `cooldown_s` | 600 | one candidate per (service, signal) per cooldown |
| `error_rate_threshold` / `_window_s` | 10 / 60s | ERROR lines per service per window |
| `latency_p95_threshold_ms` | 1000 | p95, not average; loadgen = synthetic user |
| `metric_error_ratio_threshold` | 0.2 | 5xx fraction per service (RED) |
| `correlation_window_s` | 90 | buffer candidates to collapse a cascade |
| `correlation_max_span_s` | 300 | adaptive window for non-causal edges (shared trace bypasses) |
| `max_restarts_per_hour` | 3 | per service; breach → escalate |
| `approval_timeout_s` | 900 | Tier-2 wait before escalate (no action) |
| `recovery_window_s` / `max_remediation_loops` | 180 / 2 | verify window; loops before escalate |
| `diagnosis_runs` | 2 | independent runs; disagreement → escalate |
| `dry_run` | true | safe by default; `--execute` flips it |
| `state_backend` | sqlite | `postgres` for HA |
| `action_backend` | docker | `kubernetes` for scoped-RBAC remediation |

## A2 — Code map (where every claim lives)

| Layer | Path |
|---|---|
| Cross-layer data objects | `sre_agent/models.py`, `incident/models.py`, `diagnosis/schema.py` |
| Ingestion | `sre_agent/ingest/` (`tailer.py`, `parser.py`, `window.py`) |
| Telemetry SPIs + adapters | `sre_agent/telemetry/` (`sources.py`, `adapters.py`, `promql.py`) |
| Detection | `sre_agent/detect/` (`engine.py` + each detector) |
| Secondary pollers | `sre_agent/poll/` |
| Incident mgmt | `sre_agent/incident/` (`manager.py`, `correlation.py`, `topology.py`, `lifecycle.py`, `pg_store.py`) |
| Diagnosis (LLM) | `sre_agent/diagnosis/` (`diagnoser.py`, `context.py`, `llm.py`) |
| Actions | `sre_agent/action/` (`catalog.py`, `executor.py`, `backend.py`, `k8s_backend.py`, `ratelimit.py`, `recovery.py`) |
| Integrations | `sre_agent/integrations/` (`jira.py`, `confluence.py`, `postmortems.py`, `notifications.py`) |
| State & HA | `state_factory.py`, `pgdb.py`, `pg_changelog.py`, `ha/leader.py` |
| Self-monitoring | `health.py`, `metrics.py` |
| Dashboard | `sre_agent/dashboard/` (`server.py`, `control.py`, `metrics.py`) |
| Eval | `eval/` (`harness.py`, `diagnosis_eval.py`, `scenarios.py`) |
| Deploy | `deploy/` (`k8s/rbac.yaml`, `k8s/agent-deployment.yaml`, `alerts.yml`, `prometheus-scrape.yml`) |

## A3 — Likely tough questions & honest answers

- **"What if the LLM hallucinates a root cause?"** → Evidence is fact-checked against the real logs;
  two runs must agree; the action is from a fixed catalog; the restart target is pinned by code, not
  the model. Worst case it escalates to a human — it cannot act on a fabrication.
- **"Can the AI decide something dangerous is safe to auto-run?"** → No. The tier is resolved in code
  from the action + target; the model has no tier field. Stateful = approval; unknown = escalate.
- **"Can it delete my data?"** → There is no destructive data action in the catalog, in any tier. On
  k8s its identity can't even delete a deployment or read a secret — provable via `kubectl auth can-i`.
- **"Will it spam us with false alarms?"** → 2-min debounce + cooldown + the null test (1hr healthy →
  zero output) as a hard gate. False positives are our top-severity bug class.
- **"What happens if the agent itself dies?"** → State is persisted; on the HA path a standby takes
  over mid-incident without re-acting; a dead-man's switch restarts a wedged agent.
- **"How do we adopt it without risk?"** → Shadow mode (execute nothing) → measure FP/MTTR → suggest →
  auto per-fingerprint, behind a kill switch and rate limits.
- **"What does it cost to run?"** → Detection is free deterministic code; the LLM is called only on a
  confirmed incident (typically a handful per day), so token spend is bounded and small.
- **"Vendor lock-in to a specific cloud/LLM?"** → No. Engine is substrate-agnostic; telemetry,
  actions, topology, ticketing, and the LLM are all behind swappable interfaces (MCP-first roadmap).

## A4 — Suggested deck flow / timing (≈25–30 min)

1. Slides 1–5 (story + principle) — 5 min, non-technical warm-up.
2. Slides 6–7, 11, 13 (invariants, pipeline, correlation, tiers) — 8 min, the substance.
3. **Slide 20 live demo** — 10 min, the emotional peak. Do faults 4, 6, 8 if short on time.
4. Slides 21–22 (null test + proof) — 3 min, credibility.
5. Slides 24–25 (real vs planned + limitations) — 2 min, integrity.
6. Slides 26–28 (ROI + CTA) — 2 min, close.
- Keep slides 7b, 8–10, 12, 14–19, 23 and the appendix as depth to pull from for a technical audience or Q&A.
  Pull 7b specifically when someone asks "why not multi-agent?" or pitches a competitor's "AI agent team."
```
