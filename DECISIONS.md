# DECISIONS.md — design decisions and rationale

Append-only. Newest at the bottom. Format: ID, date, decision, why, consequences.

---

## D-001 · 2026-06-11 · No LLM in the detection loop
Detection is deterministic code (thresholds, debounce, cooldown). The LLM runs only after
an incident is confirmed.
**Why:** cost, latency, and reliability — an always-on LLM watcher is flaky and expensive;
false positives kill SRE tools.
**Consequence:** detectors must be individually testable against recorded log fixtures.

## D-002 · 2026-06-11 · Topology-aware correlation before ticketing
Candidates within 90 s sharing an upstream dependency merge into one incident attributed
to the most upstream suspect.
**Why:** a single Redis OOM otherwise produces separate api/worker/queue incidents — the
predicted #1 failure mode of a naive v1. One fault must equal one ticket.
**Consequence:** the topology map is a load-bearing input and must be kept current.

## D-003 · 2026-06-11 · Enumerated action catalog; tiers hard-coded in code
The LLM selects an action id from a whitelist; the risk tier is a property of the action,
enforced by code. The diagnosis schema has no `action_tier` field.
**Why:** a confident-but-wrong model must never be able to classify a risky action as
auto-executable. The diagnosis→action joint is the most dangerous part of the design.
**Consequence:** new remediations require a code change (catalog entry + tier + guardrails),
which is the point.

## D-004 · 2026-06-11 · No confidence-based routing
LLM confidence numbers are not calibrated and are not used for tier/escalation decisions.
Escalation triggers instead: unknown fingerprint, evidence refs that don't resolve to real
log lines, disagreement between two independent diagnosis runs, invalid action selection,
tripped guardrails, repeated verification failure.
**Why:** models report 0.9 confidence on plausible wrong answers.
**Consequence:** diagnosis may run twice per incident (agreement check) — acceptable cost.

## D-005 · 2026-06-11 · Per-fingerprint recovery predicates
Verification evaluates concrete conditions (e.g. p95 < X AND error rate < Y AND queue
depth decreasing), defined per fingerprint, evaluated by code.
**Why:** "watch 3 min, looks normal" auto-resolves partial recoveries, destroying trust.
**Consequence:** every fingerprint definition ships with its predicate.

## D-006 · 2026-06-11 · Integrations behind interfaces; stubs first
Ticketing, post-mortems, and notifications are interfaces. Defaults: SQLite ticket store,
markdown post-mortems, console notifications. Real JIRA/Confluence/Teams swap in late.
**Why:** three external integrations are pure plumbing that would otherwise eat half the
project before the interesting layers (1–5, 9) work.
**Consequence:** interface design must match JIRA/Confluence/Teams semantics closely
enough that the swap is mechanical (states, comments, assignees).

## D-007 · 2026-06-11 · Agent actions are tagged in the change log; detection ignores them
Every agent action is logged with `actor: sre-agent` before execution; detection suppresses
anomalies inside the expected blast radius/time window of a tagged action.
**Why:** the agent's own restarts look like crashes; without this it diagnoses its own
remediation.
**Consequence:** each catalog action declares an expected blast radius.

## D-008 · 2026-06-11 · FLAPPING is an explicit state
A fingerprint that re-fires within 30 min of resolution reopens its ticket once and holds
it open; further oscillations are comments; 3 cycles → escalate.
**Why:** debounce + reopen logic otherwise oscillate resolve/reopen on a fault that
recovers and re-fails every few minutes.

## D-009 · 2026-06-11 · Incident state persisted; actions idempotent
Incident state lives in SQLite; on restart the agent resumes without re-executing. Actions
check current reality (container state, queue state) before executing.
**Why:** an agent crash mid-ACTING must not double-restart services or double-flush queues.

## D-010 · 2026-06-11 · Static baseline is acceptable for the lab
Error-rate baselines are learned from a healthy-traffic window and treated as static,
because loadgen produces a stationary workload.
**Why:** honest scoping — this does not transfer to real systems (diurnal patterns,
deploy-shifted baselines) and is explicitly a demo simplification.

## D-011 · 2026-06-11 · Eval harness built alongside layer 1, not last
The harness skeleton exists from the first milestone; every detector/action/scenario ships
with a scored eval scenario in the same change.
**Why:** otherwise testing is manual for weeks and the harness becomes an afterthought.

## D-012 · 2026-06-11 · Token-budget priority order for diagnosis context
Correlated request traces > change log > error window > neighbor logs > topology >
post-mortem memory, truncated in that order.
**Why:** with the wrong ordering the model reads thousands of tokens of healthy access
logs before the signal.

## D-013 · 2026-06-12 · Total stream blackout is one `stream_blind` signal, not N silences
When every service the agent has ever seen goes quiet simultaneously, the silence detector
emits a single `stream_blind` anomaly (service `_stream`) instead of one `silence` per
service.
**Why:** a dead tailer, Docker hiccup, or stopped lab makes the agent *blind* — it is not
N independent service outages. Surfaced by the failed M0 null test, which emitted four
simultaneous per-service silence candidates when the lab simply wasn't running. Paging once
per service for a single blind condition is exactly the alert-storm the design forbids.
**Consequence:** the Incident Manager (M2) must route `stream_blind` as an agent-health
alert, not a lab incident; a partial silence (some services down, others healthy) still
produces ordinary per-service `silence` candidates.

## D-014 · 2026-06-12 · Multi-signal fault scenarios deferred to M2; unit tests exercise detectors meanwhile
The latency p95, crash-loop, and queue-growth detectors ship with unit tests but without
live eval-harness scenarios. CLAUDE.md requires a scenario per detector; the spirit
(a test that exercises it) is met by recorded-fixture unit tests.
**Why:** the lab's richer faults are inherently multi-signal — `latency` trips both
latency_p95 and error_rate (504 timeouts); `memleak` trips both crash_loop and error_rate
(OOM gap). The current harness scores any non-primary candidate as a failure, so it cannot
fairly grade a multi-signal fault until M2's topology correlation collapses the symptoms
into one incident with a single primary signal. `queue_growth` has no direct chaos toggle
at all (kill-worker also kills the heartbeats → silence catches it).
**Consequence:** when M2 lands correlation, add `latency` and `memleak` live scenarios and
score by primary signal, not candidate exclusivity. Tracked in eval/scenarios.py
(DEFERRED_TO_M2).

## D-015 · 2026-06-12 · Pollers inject their IO and degrade to None, never raise
Each secondary-signal poller (docker inspect, health, redis depth, pg connections) takes
its IO as an injected `run`/`probe` callable and catches all failures, returning None /
unhealthy for that field. PollCycle additionally isolates a raising poller so one broken
source can't blank the others.
**Why:** (1) testability — poller logic is unit-tested with fakes, no real Docker/Redis
needed; (2) the secondary signals are *context*, and a momentarily unreachable source must
never crash the agent or fabricate data. A missing field is honest; a wrong field is a lie
the diagnosis layer would later trust.
**Consequence:** live subprocess glue lives in poll/adapters.py (verified against the lab,
not unit-tested); detectors that consume polled signals (e.g. a redis-depth detector that
sees a backlog even when the worker is silent) are a later addition.

## D-024 · 2026-06-12 · M7 real Jira + Confluence behind the existing interfaces, opt-in
`JiraTicketStore` (TicketStore) and a Confluence `ConfluencePublisher` + `CompositePostMortemStore`
(PostMortemStore) implement the same interfaces as the SQLite/markdown stubs. Enabled with
`--jira` / `--confluence`; SQLite stays the default so all local demos and the 226 tests run
untouched. Credentials live in gitignored `.secrets/atlassian.env`.
**Jira:** standard issue REST v3 (ADF bodies), `[System] Incident` type in project HELP;
fingerprint stored as a label for JQL dedup; lifecycle mapped onto the project workflow
(Open / Work in progress / Pending / Completed) via the transitions API; severity → priority.
HTTP transport is injected so mapping is unit-tested; validated live (HELP-1 created, commented,
transitioned, resolved).
**Confluence is a publish target, not the query source:** incident-memory reads must be fast,
so a local SqlitePostMortemStore stays authoritative and CompositePostMortemStore tees each
record to Confluence best-effort — a Confluence outage never blocks the agent. Validated live
(page published to space ReliAxiom). Teams notifications deferred per the user.
**Consequence:** the manager is unchanged — it still talks to the interfaces; only `main`'s
backend selection differs.

## D-023 · 2026-06-12 · Unknown-fingerprint escalation is opt-in (off by default)
The AGENT.md "fingerprint not seen before → escalate" trigger (deferred from D-018) is
implemented as `escalate_unknown_fingerprint`, **off by default**. When on, a fault with no
prior post-mortem is held for a human; once it has been seen (a post-mortem exists), the
agent auto-acts.
**Why:** hard-escalating every first occurrence would block all first-time auto-remediation
(e.g. the kill-worker happy path), defeating the autonomous core. Incident memory (D-022) is
the primary mechanism — the agent gets *more* capable as it sees faults, rather than refusing
novel ones. The flag exists for operators who want a stricter, supervised-first posture.
**Consequence:** the novelty check runs only on the first attempt (not retries), keyed on the
post-mortem archive.

## D-022 · 2026-06-12 · M6 incident memory: post-mortems feed back into diagnosis, placed high
Every resolved/escalated incident writes a post-mortem (structured + markdown). The diagnosis
context queries past post-mortems for the same fingerprint and injects them as `## INCIDENT
MEMORY`, placed right after the incident header (not last as D-012 implied for "post-mortem
memory").
**Why:** "have we seen this before and what fixed it?" is the strongest prior a diagnostician
has — it belongs near the top, not at the truncation tail. Exit criterion proven: the second
occurrence of a fault is diagnosed with the prior post-mortem in its prompt.
**Consequence:** post-mortems are a SQLite store (queryable) plus markdown files (the human
artifact and Confluence-swap target at M7); a `health_report` aggregates them.

## D-021 · 2026-06-12 · Transient LLM failure retries; only a content failure escalates
A provider failure (429/timeout) sets `provider_unavailable` on the diagnosis; the manager
leaves the incident in DIAGNOSING and retries on later cycles, escalating only after
`max_diagnosis_attempts`. A *content* failure (malformed JSON, bad evidence, invalid action)
escalates immediately.
**Why:** a rate-limit is transient — permanently escalating an obviously-fixable fault (worker
down) because the LLM blinked is wrong. The agent should self-heal when the provider returns.
A content failure won't fix itself, so it escalates at once.
**Consequence:** during an LLM outage incidents wait (DIAGNOSING) rather than pile onto on-call;
they auto-remediate once the LLM is reachable.

## D-020 · 2026-06-12 · M5 HITL: Tier-2 actions wait for human approval; timeout escalates
A diagnosis selecting an APPROVAL-tier action is not executed — the manager transitions the
incident to AWAITING_APPROVAL, persists the pending action, posts a proposal with the action's
blast radius (target + topology dependents), and waits. A human approves (`python -m
sre_agent.approve approve INC-n --by <who> --execute`) → the action runs and the approver is
recorded on the ticket → VERIFYING. Reject → ESCALATED. No response within approval_timeout_s
(15 min) → ESCALATED with NO action taken (checked in `step`).
**Why:** invariant — anything touching state/config/multiple services needs a human. Blast
radius comes from the topology so the approver sees what a redis restart actually affects.
The pending action is persisted so approval survives an agent restart.
**Consequence:** approval arrives out-of-band via the CLI (or, at M7, a Teams button) acting
on the shared SQLite state; the running agent's next verify cycle picks up the approved
incident. Exit criteria met at integration level (HITL timeout + Tier-2 approval happy path);
live Redis-OOM-with-approval reuses the already-validated diagnosis path.

## D-019 · 2026-06-12 · M4 actions: dry-run by default; guardrails and recovery enforced in code
The action executor defaults to dry-run (log intent, don't execute); live execution requires
`--execute` / `dry_run=False`. Guardrails (only AUTO auto-executes, restart cap per service
per hour counted from the change log, never postgres data) and per-fingerprint recovery
predicates are code-enforced. The manager's `step()` orchestrates detect→diagnose→act→verify
in one cycle; a blocked guardrail or a Tier-2/3 action escalates (no approver path until M5).
**Why:** safe-by-default — prove the decision path in dry-run before letting the agent change
the lab. Tagging each action in the change log before executing lets detection ignore the
transient it causes (invariant #5), verified by an integration test (one incident, never a
self-induced second).
**Consequence:** the M4 live exit criterion (kill-worker auto-remediated end-to-end with
`--execute`) requires a working LLM diagnosis; see the quota note below.

## D-018 · 2026-06-12 · M3 escalation triggers: evidence/agreement/action now; unknown-fingerprint deferred
The diagnoser escalates on: hallucinated evidence (cited refs not found in the window),
disagreement across independent runs, an invalid/unknown action, or malformed output. The
AGENT.md trigger "fingerprint not seen before / no runbook match" is **deferred to M6**.
**Why:** without post-mortem memory (M6) every fingerprint is unknown, so escalating on that
now would escalate every incident and make M4's auto-remediation untestable. Evidence
verification is a token-overlap check: a cited ref must share a ≥5-char token (request id,
error code) with the real log corpus, so a fabricated request id is rejected.
**Consequence:** when M6 lands incident memory, add the unknown-fingerprint trigger keyed on
"no matching post-mortem". Tier is always resolved by the action catalog, never the model
(invariant #2); routing never uses a model confidence (D-004).

## D-017 · 2026-06-12 · Diagnosis goes through an LLMProvider interface; Gemini now, Anthropic later
The diagnosis layer depends only on `LLMProvider.complete(system, user) -> text`. The current
implementation is `GeminiProvider` (Google AI Studio, gemini-flash-latest), because that is
the credential available; an Anthropic Claude provider swaps in later with no change to the
diagnosis layer. HTTP transport is injected (testable offline). The API key is read from an
env var (`GEMINI_API_KEY`), loaded from gitignored `.secrets/llm.env` — never committed,
never in a config file.
**Why:** the user provided Google AI Studio creds and will move to ANTHROPIC_API_KEY later.
This deviates from the CLAUDE.md default ("prefer Claude models"); the interface keeps the
deviation cheap to reverse — building the Anthropic provider is one class implementing the
same method. No LLM specifics leak past the boundary.
**Consequence:** still honors invariant #1 — the LLM is invoked only after an incident is
confirmed (M3 diagnosis), never in the detection loop. Provider selection is config-driven
(`llm_provider`).

## D-016 · 2026-06-12 · Correlation keys on dependency error codes, not shared-dep alone
Refines D-002. Correlation attributes a fault in priority order: (1) dependency error codes
in the evidence (`redis_unreachable` → redis) pull in every candidate that depends on that
root; (2) topology — among the rest, the service all others depend on is the root; (3)
leftovers stand alone. Candidates that *merely share a common dependency* are NOT merged on
that basis. Candidates carry `error_codes` (added to Anomaly/IncidentCandidate; populated by
the error-rate detector from each record's `error` field).
**Why:** D-002 said "share an upstream dependency," but worker and gateway both depend on
redis+postgres, so naive shared-dep merging would fuse every unrelated blip between them. A
genuine shared-dependency outage announces itself with an error code (redis_unreachable /
db_unreachable in this lab), which is a precise signal; topology is the fallback for
single-service cascades (api 500s rippling to its dependents).
**Consequence:** new dependency faults need an entry in correlation.DEP_ERROR_ROOTS mapping
their error code to (root_service, fault_type). `stream_blind` depends on nothing in the
topology, so it never merges into a lab incident — correct, it is an agent-health signal.


## D-025 · 2026-06-13 · Demo dashboard is a read-only projection over the persisted stores
A FastAPI + static-SPA dashboard (`sre_agent/dashboard/`) renders the agent's state live for a
client demo. It opens the existing SQLite stores (incidents, tickets, change log, post-mortems)
**read-only and out-of-process** and never imports or drives the detection/diagnosis/action
core. Live updates are a full-state snapshot pushed over SSE every ~1.5s (polling the DBs, not
the agent). Live container/queue gauges come from the dashboard's *own* best-effort poll
(`build_live_pollers`), because the agent keeps its SignalStore in memory; if Docker is absent
the gauges degrade and the rest of the dashboard still works. ROI metrics (`dashboard/metrics.py`)
are pure aggregation over the post-mortem archive + active incidents, unit-tested offline.
**Why:** the engine was demo-ready but invisible — only stderr heartbeats and `sqlite3`
one-liners. A client must *see* it; making the dashboard a pure projection keeps invariant #6
(false positives are bugs) untouched — the read layer can't perturb detection.
**Consequence:** the dashboard reflects only what the agent has persisted; anything it should
show must first be written to a store by the core. New deps live behind the `dashboard` extra.

## D-026 · 2026-06-13 · Provider fallback chain so a free-tier 429 can't kill a live demo
`FallbackProvider` (`sre_agent/diagnosis/llm.py`) wraps an ordered list of providers and moves
to the next on any `LLMError`. `build_provider` chains the secondary provider behind the primary
whenever both API keys are present (`llm_fallback`, on by default): OpenRouter → Gemini.
**Why:** diagnosis runs on free tiers that rate-limit; a 429 mid-demo would otherwise force a
Tier-3 escalation and break the narrative. Each underlying provider still exhausts its own
transient-retry budget first, so reaching the fallback means that provider is genuinely down.
**Consequence:** honors invariant #1 (LLM only after a confirmed incident) and the tier rules —
the fallback changes *which* provider answers, never how the answer is routed or tiered.


## D-027 · 2026-06-13 · Demo topology expansion: auth + payments tiers (chaos-based faults)
Added two stateless services to the lab to make the managed system look production-grade and
to show off topology correlation on the dashboard: `auth` (the api authenticates every request
via auth/verify) and `payments` (the worker charges every order via payments/charge). Topology:
api→auth, worker→payments (both leaf deps, like postgres/redis). New error codes
auth_unreachable / payments_unreachable map to their roots in `DEP_ERROR_ROOTS`; both services
are stateless (auto-restart, Tier 1). Two eval scenarios added (auth-down, payments-down).
**Faults are chaos-toggle based, not `docker stop`:** stopping a container the api/worker call
over HTTP makes DNS resolution of the dead name hang ~3 s (the requests timeout can't cap DNS),
which trips the latency detector and muddies a clean error_rate cascade. A chaos toggle (the
dependency stays up but returns 5xx) fails fast → a single-signal error_rate cascade, and
`restart_container` still remediates it because the chaos flag lives in process memory.
**Why:** a believable multi-tier graph + a marquee cascade (auth failing → api/webapp/gateway/
loadgen all error → collapses to one auth:unreachable incident → auto-restart → resolve) is the
demo's centrepiece. The api/auth and worker/payments calls fail fast (0.5 s / 1 s timeouts) so a
dependency blip never turns into user-facing latency.
**Consequence:** validated live on the expanded stack — null test silent; auth-down detected in
130 s, payments-down in 140 s; existing scenarios unaffected. The dashboard renders the new
nodes automatically (it reads LAB_TOPOLOGY). 248 offline tests green.


## D-028 · 2026-06-13 · Dashboard becomes the demo control plane; Jira/Confluence default-on
The dashboard gains a write side so it is the single pane of glass — the only terminal role is
`docker compose up` for the lab. New control endpoints: start/stop the agent as a managed
subprocess (`AgentRunner`, launched `--execute --jira --confluence`, live log streamed),
inject/reset demo faults (`DEMO_FAULTS` → lab chaos toggles / docker), and approve/reject Tier-2
incidents (via `IncidentManager.approve/reject`, the same path the approve CLI uses). Default
integration backends flipped to `jira` + `confluence` (`config.py`); the composite stores keep a
local SQLite/markdown mirror and degrade to it if Atlassian is unreachable, so the default is
safe offline and tests still pass. Incident summaries carry a `jira_url` (`{site}/browse/{key}`)
because the composite store uses the Jira key as the ticket id.
**Why:** the user wants to demo from a UI, not terminals, with real Jira/Confluence by default.
**Boundary kept:** the read side stays a pure projection; the control side is orchestration only
(it drives the lab and the existing approval API) — neither changes detection/diagnosis logic,
so invariant #6 (silence on healthy systems) is untouched. Validated live: Start agent → inject
auth-down → one Jira ticket (HELP-6) → auto-restart → resolved; db-down → Tier-2
AWAITING_APPROVAL → approve in UI → postgres restarted → resolved. Faults that overlap without a
reset produce extra incidents (known multi-signal correlation limit), so the demo resets between
scenarios. 254 offline tests green.


## D-029 · 2026-06-13 · restart_container's target is the correlated root_service, not the model's pick
For `restart_container` the manager pins `action_params["service"]` to the incident's
`root_service` (the deterministically-correlated root) before the tier is resolved, via a new
`restart_target` arg on `Diagnoser.diagnose`. The LLM still selects the action *id*; it no longer
chooses *which* service to restart.
**Why:** the bug that surfaced this — postgres/redis outages never reached AWAITING_APPROVAL in
the UI. On a `docker stop postgres`, correlation correctly roots the incident at postgres
(stateful ⇒ Tier-2), but the LLM sees the errors in api/worker logs and frequently proposed
`restart_container {service: api}` — which resolves to Tier-1 AUTO. The agent then silently
auto-restarted the wrong, stateless service and never asked for approval. Letting the model name
the service let it *downgrade* a Tier-2 stateful restart to a Tier-1 stateless one — a back-door
violation of invariant #2 (the model must not set the tier). Which service is the culprit is
already established deterministically by topology correlation; re-deriving it from the model was
both redundant and unsafe. Pinning also fixes a missing/disagreeing-service escalation and makes
the agent restart the *root* (e.g. auth) instead of the symptom on every cascade.
**Consequence:** for any restart the agent acts on the correlated root; the diagnosis_eval live
path pins the same way so it scores real agent behavior. 260 offline tests green (6 new).
**Follow-up (caught live):** pinning the restart to the root surfaced that `loadgen` — a stateless
lab service and the sole `latency_services` member — was missing from the catalog's
`STATELESS_SERVICES`, so every loadgen-rooted incident (latency_p95, silence) hard-escalated with
"unknown service 'loadgen'". Added `loadgen` to `STATELESS_SERVICES` (Tier-1) and a guard test
(`test_every_lab_service_is_restartable`) asserting every topology service is a known restart
target, so a future topology addition can't silently fall out of the catalog again.


## D-031 · 2026-06-13 · Strategic direction: substrate-agnostic engine + MCP as the universal connector
The product evolves from a lab-specific agent into a platform that connects to **any application
in any cloud or on-prem** without forking the engine. The architecture is three rings (see
[VISION.md](VISION.md)): **Ring 0** the substrate-agnostic engine (already built — detect /
correlate / state machine / diagnose / tier / verify / HITL / post-mortem operate only on
abstract objects); **Ring 1** per-environment adapters behind Provider SPIs (`TelemetrySource`,
`ActionBackend`, `TopologyProvider`, `ChangeFeed`, plus the existing TicketStore / Notifier /
PostMortemStore); **Ring 2** connectors, with **MCP servers as the primary wire** and native
SDK/REST adapters where no MCP server exists. The agent becomes both an MCP **client** (consume
telemetry/action/ITSM servers) and, later, an MCP **server** (expose incident state + gated
approve tools to an agent mesh). Priority order is in [ROADMAP.md](ROADMAP.md) Part II (P0–P3).
**Why:** the engine's value is that it is environment-independent; the only thing stopping
"works on the lab" from becoming "works on your stack" is the connector layer, and MCP is an
open, discoverable, credential-scoped substrate already proliferating across exactly the
observability/cloud/ITSM systems an SRE agent must touch. Reuses the proven stubs-before-real,
interface-backed discipline (D-006/D-024) rather than inventing a new pattern.
**Consequence — invariants are reinforced, not relaxed, when actions arrive over MCP:** an MCP
tool is *transport, never policy* — it is mapped into the enumerated catalog with an
operator-assigned tier (never auto-tiered; unmapped tools are inert), so a confident-but-wrong
model still cannot invoke a risky tool (#2; D-003). Telemetry MCP servers feed deterministic
detectors only (#1). Every MCP action is change-log-tagged before execution (#5/#7). No MCP tool
in any tier performs a destructive data-of-record operation, enforced at the catalog mapping
(#4). Telemetry pulled via MCP is untrusted input to diagnosis, so the evidence-grounding /
double-run guards (D-004/D-018) and tiers-in-code remain the backstop. Every environment onboards
through the shadow → suggest → auto trust tiers (D-019/D-022), gated by the P1 meta-monitoring
false-positive/MTTR signals. **Nothing here is implemented yet — it is the recorded plan.**


## D-030 · 2026-06-13 · Tolerate the model echoing an action's display form as its id
The diagnoser strips a trailing parameter hint from `selected_action` (e.g.
`restart_container(service)` → `restart_container`) before resolving, and `catalog_summary`
no longer renders ids glued to their params (`- restart_container: <desc> (required params:
service)` instead of `- restart_container(service): <desc>`).
**Why:** observed live — on a `loadgen:silence` (a trivially auto-fixable stateless restart) the
LLM copied the catalog's `restart_container(service)` display token verbatim as the action id;
the catalog lookup failed ("unknown action") and the trust guard escalated a fault the agent
should have fixed itself. Letting a cosmetic prompt-formatting artifact trigger a Tier-3
escalation needlessly pages a human and erodes invariant #6's spirit. The id is still validated
against the catalog (invariant #2 intact) — only an obvious display-form artifact is normalized.
**Consequence:** 263 offline tests green (3 new).


## D-032 · 2026-06-13 · TelemetrySource SPI (P0.1): detectors read logs/metrics/traces through interfaces
The detection layer no longer depends on the concrete `SlidingWindow`. A `sre_agent/telemetry/`
package defines three read SPIs — `LogSource` (the four methods detectors already used:
`services`/`last_seen`/`records`/`error_records`), `MetricSource` (instant + range PromQL), and
`TraceSource` (per-service error rate + trace-by-id). Every detector and `DetectionEngine.tick`
now type against `LogSource`; `SlidingWindow` is the default in-memory impl (a `runtime_checkable`
Protocol conformance test pins this). Live adapters (`telemetry/adapters.py`) — `PrometheusMetricSource`,
`LokiLogSource` (reuses the tolerant `LineParser`), `TempoTraceSource` — inject their HTTP transport
and **degrade to None/empty on any failure, never raise** (extends D-015). `build_live_telemetry_sources(cfg)`
selects backends; `main.build_engine` appends metric/trace detectors only when their backend is on,
and the detector log source is Loki when `telemetry_logs == "loki"` else the window.
**Why:** the single tailed log stream is the lab simplification flagged in VISION.md/ROADMAP P0.1 —
real detection leans on metrics (a Prometheus histogram p95, not a log-derived guess) and traces.
The seam half-existed (D-006/D-015); this completes it for the four telemetry signals without
touching detection logic. **Invariants held:** detection stays deterministic (#1) — sources feed
code thresholds, never the LLM; a down backend degrades silently (#6, no fabricated anomaly).
**Consequence:** defaults keep the lab on the tailed path (`telemetry_*` off), so all offline tests
ran untouched; 290 green (26 new: adapters, metric/trace detectors, Protocol conformance).

## D-033 · 2026-06-13 · Lab instrumented with the free Grafana LGTM stack (no API keys)
`logs-streaming-demo-app` had zero observability (JSON logs to stdout only). Added, self-hosted in
its compose: Prometheus (scrapes `/metrics`), Loki + Alloy (ships Docker logs), Tempo + OTel
Collector (OTLP traces), Grafana, cAdvisor. The Flask services gained a ~15-line `prometheus_client`
RED block (`http_requests_total`, `http_request_duration_seconds` on `/metrics`); the worker exports
`queue_depth` + `jobs_processed_total`. OTel is **auto-instrumentation, opt-in per container**
(`OTEL_ENABLED=1`; the Dockerfile CMD falls back to plain `python app.py` so a missing collector can
never stop a service booting).
**Why:** the user chose the full RED/USE + traces path; instrumenting the services gives real
histograms/spans rather than log-derived metrics. Everything is free OSS on localhost — no account,
token, or cost — which is the whole point of the "what do you need to provide" answer: nothing paid.
**Consequence:** the agent runs against real backends with `telemetry_metrics=prometheus`,
`telemetry_traces=tempo` (and optionally `telemetry_logs=loki`); validated live on the full stack —
`errors-red` detected via `metric_error_ratio` (api 41% / webapp 84% 5xx) in 143s, real api histogram
p95 ≈ 59ms in Prometheus, all five services emitting traces to Tempo.
**Live-bring-up gotchas (fixed):** (1) `opentelemetry-instrument` imports `pkg_resources`, which
`python:3.12-slim` no longer ships and which **setuptools ≥81 removed** — so the OTel services
crash-looped until the Dockerfiles pinned `"setuptools<81"`. (2) Recreating a service behind the
nginx `gateway` leaves nginx resolving the old container IP → 502s until `docker compose restart
gateway`; not an instrumentation fault, a compose-recreate artifact.

## D-034 · 2026-06-13 · Metric/trace detectors are deterministic; metric-path eval scenarios added
New detectors `MetricErrorRatioDetector` (RED-errors), `MetricLatencyDetector` (RED-duration, real
histogram p95), `SaturationDetector` (USE, queue gauge) and `TraceErrorDetector` query their source
with a **fixed PromQL/TraceQL string + a code threshold** — no LLM, no model-set tiers. They emit the
same `Anomaly` objects, so debounce/cooldown/correlation are unchanged, and the log + metric twins of
one fault collapse into a single incident (#3). The eval harness gained telemetry-aware `_LiveAgent`,
applies a scenario's `extra_config` (to flip telemetry on), and honors a new `allow_signals` field so
a co-firing twin isn't scored as a false positive. Scenarios `errors-red` and `latency-red` exercise
the metric path; `latency-red` is the metric-path resolution of D-014's deferral (the histogram p95
is a clean single signal where the log path was too multi-signal to score).
**Why:** telemetry is *untrusted input* to detection — keeping the queries/thresholds in code is the
backstop (VISION §4). **Consequence:** these scenarios require the LGTM stack up; existing log-only
scenarios are unchanged. Follow-ups remain P0.2 (back these adapters with MCP servers) and P1.1 (fuse
the three signals in the correlation engine).


## D-035 · 2026-06-13 · Metric-detected faults are verified on the same metric (close the detect→fix→verify loop)
`RecoveryEvaluator` takes an optional `MetricSource`. A `metric_latency_p95` incident is recovered
only when the Prometheus p95 is back under threshold; `metric_error_ratio` only when the 5xx ratio is;
`metric_saturation` only when the queue gauge is — each with a log sanity check. The PromQL is built
from one shared module (`telemetry/promql.py`) used by both the detectors and the verifier, so detect
and verify can never drift. The metric source **degrades to the log-based checks** when absent or
unreachable: a momentarily-down backend can't wedge an incident open, and it can't declare a premature
victory either.
**Why:** the gap caught while reviewing fix-vs-escalate — detection had moved to metrics but
verification still judged a latency fault on whether *error logs* cleared, so a still-slow service
could be marked recovered. Verifying on the signal you detected on is the robust closure of D-005.
**Tier policy unchanged (answers "fix or escalate"):** the action catalog still sets the tier in code
(invariant #2) — stateless restarts auto-fix (Tier 1), stateful redis/postgres need approval (Tier 2),
guardrail breach / unknown action / repeated verify-failure escalate to a human. Telemetry only adds
detection/verification signals; it never routes. **Consequence:** 299 offline tests green (+9: metric
recovery, the offline null-test encoding for the full log+metric+trace engine, shared PromQL). The
running agent gets metric-aware recovery automatically (the dashboard launches it with the telemetry
config; `RecoveryEvaluator` is wired with `telemetry.get("metrics")`). Trace-based recovery (verify on
Tempo error-rate) is a later refinement; `trace_error_rate` currently verifies via the log path.


## D-036 · 2026-06-13 · Container-down is a first-class detection signal (stateful outages don't need a log flood)
Detection was 100% log/metric/trace-rate based, so "a dependency is down" only became a ticket if its
consumers logged *enough* errors to trip a threshold. Two real outages don't: **redis** has one
consumer (the worker) that retries on a slow fixed cadence (~one error every few seconds — below
`error_rate_threshold`), and **postgres down while the auth tier is also failing** never produces a db
error at all because requests 503 at auth before reaching the db. The poller already *knew* the
container was down (the heartbeat prints `DOWN=postgres`), but nothing turned that fact into a
candidate. New `ContainerDownDetector` reads the polled `SignalStore` and emits an `Anomaly` for any
container in `cfg.container_down_services` (default `postgres`, `redis` — exactly the stateful deps the
silence detector excludes) tagged with that dependency's **error code** (`db_unreachable` /
`redis_unreachable`), so the correlator roots it on the same `service:unreachable` fingerprint the log
path uses — a container-down candidate and any downstream error-rate candidate collapse into ONE
incident (#3). It runs through the normal engine, so it inherits debounce, cooldown, and
action-suppression (it will not alarm on the brief down-state the agent's own restart causes — #5).
`RecoveryEvaluator` gained a root-container-up gate for `unreachable` faults: when the container-down
candidate is the only evidence the log checks are vacuously clean, so the live signal must veto a
premature recovery (and confirm it once the operator/agent restarts the container). The dashboard
topology/health now reflect the live poll too — a stopped lab service renders red before an incident
exists and clears the instant it's restarted (previously the map was incident-only, so a `docker stop`
looked like "nothing happening"). Eval `_LiveAgent` is now poll-aware; scenario `db-down` (stop
postgres) exercises the detector.
**Why:** detection is pure code (invariant #1) and the down-container fact is the most deterministic
signal there is — keying stateful-outage detection off downstream log volume was the fragile path.
This is the robust closure of the gap behind "I stopped postgres and no ticket appeared."
**Also hardened in the same pass (operational robustness, not new behavior):** (1) the agent's main
tick loop now catches/logs/continues on a per-tick exception — a transient `database is locked`, a
telemetry blip, or an LLM hiccup can no longer silently freeze detection (a wedged agent that stops
detecting is the failure this project exists to avoid). (2) Every SQLite store opens through one
`sre_agent/db.py` helper with **WAL + a 30s busy timeout**, so the out-of-process dashboard's reads and
the approval path's writer connection no longer contend with the agent's writes into a lock error.
**Consequence:** 312 offline tests green (container-down detector + correlation, root-container-up
recovery, live-signal topology). `db-down` needs the lab up (it stops a real container).


## D-037 · 2026-06-14 · ActionBackend SPI — remediation is substrate-pluggable, not docker-on-the-host
The action layer shelled `docker restart` over the local socket (≈ root on the host, single-host, name-
addressed) and treated the CLI exit code as success — fine for the lab, disqualifying for anyone else's
system. New `ActionBackend` Protocol (`supports`/`observe`/`apply`, P0.1) makes execution a swap behind
the same discipline the telemetry sources use (D-031): the engine selects a catalog action *id*; a
backend translates it to a substrate primitive and is the *only* thing that knows the substrate.
`ActionExecutor` is now a thin orchestrator (dry-run/shadow gate → capability gate → delegate); the
docker argv lifted verbatim into `DockerActionBackend` (lab default, behavior-identical). The
production-shaped backend is `KubernetesActionBackend`, run against a **free local `kind` cluster** (no
cloud): a restart is a declarative, **idempotent** strategic-merge PATCH (the `rollout restart`
mechanism), and recovery is **verified against the Deployment's readyReplicas/observedGeneration**, not
an exit code. It authenticates as a ServiceAccount with a **least-privilege Role** — exactly
`get/list/patch` on Deployments in one namespace (deploy/k8s/rbac.yaml), so the blast radius is an
explicit RBAC grant, not host-root. Transport is injected (canned-JSON unit tests; wire shapes
integration-validated like the Tempo adapter); `observe()` degrades to None on failure.
**Invariants preserved:** tiers stay in code (#2) — a backend only *declares which ids it supports*,
unsupported ids are inert (the seam MCP action servers will plug into, P0.2); the change-log tag is
still written before execution (#5); no backend performs a destructive data op (#4 — the k8s Role omits
delete/exec/secrets deliberately). **Consequence:** the same kill-worker scenario M4 proved on docker
now runs end-to-end through the k8s backend (`test_pipeline_actions_p0`), proving the SPI doesn't perturb
the engine. `action_backend` config selects the substrate; `scripts/setup-kind.ps1` brings the cluster up.


## D-038 · 2026-06-14 · The restart cap is atomic and fleet-safe, enforced in an ActionRateLimiter (not Guardrails)
The per-service restart cap counted a local SQLite change log in `Guardrails` and *then*, in a separate
write, the manager recorded the action tag — a check-then-act gap two replicas (or two ticks) could both
pass, so the fleet exceeded the cap exactly during a fault, when a runaway agent is most dangerous. Worse,
the same per-process change log also enforces invariant #5 (suppress detection on the agent's own
action), so a per-process store let replica B alarm on / re-remediate replica A's restart. The cap is a
*safety* invariant, so a cap that doesn't hold fleet-wide is a correctness bug, not a nicety. New
`ActionRateLimiter` SPI: `try_consume` performs the window count **and** writes the #5 tag in **one
atomic transaction**, so concurrent callers serialize and the cap is exact. `Guardrails` keeps only the
non-rate gates (valid + AUTO tier). Default `SqliteRateLimiter` uses `BEGIN IMMEDIATE` on the shared
change-log DB (correct for multiple processes on one host); a concurrency test (20 threads, one store)
proves exactly `cap` succeed. **No cloud:** the multi-host swap is `guardrail_store='postgres'` behind
the same SPI — the impl lands with HA (P1.2) and raises `NotImplementedError` until then rather than
silently degrading to a per-host cap. **Note (invariant #4):** that future store is the agent's own
*control-plane* DB, never the monitored system's data store — a separate instance, deliberately. The
limiter reads/writes the same `changes` table `ChangeLog` does, so it counts restarts from any path
(auto or approved) and its tags stay visible to diagnosis and detection-suppression.
**Consequence:** 346 offline tests green; the cap and the #5 tag are now one atomic, store-scoped fact.


## D-039 · 2026-06-14 · TopologyProvider SPI — the dependency graph is a swappable source, not a literal
The last two P0.1 read seams were still hardcoded literals the engine imported directly: the dependency
graph (`LAB_TOPOLOGY` in `incident/topology.py`, imported into `main`, `approve`, both dashboard modules)
and the change feed. This closes the topology half. New `TopologyProvider` Protocol (`topology() ->
TopologyMap`, `runtime_checkable`) makes *where the graph comes from* a swap behind the same discipline as
the telemetry sources (D-032) and pollers (D-015) — correlation, the diagnosis context, and the dashboard
keep consuming an unchanged `TopologyMap`. Default `StaticTopologyProvider` wraps the built-in
`LAB_TOPOLOGY`. The production-shaped `HttpTopologyProvider` fetches a `{service: [deps]}` adjacency
document (service-mesh / trace-graph / CMDB / Backstage), **injects its HTTP transport** (parsing
unit-tested offline against canned JSON), and **degrades to the static fallback on any failure** — a 4xx/5xx,
malformed JSON, a wrong-shape body, or a raising transport all return the fallback, never crash correlation
or fabricate a graph. A successful fetch is cached (topology is queried per candidate); a failure is *not*
cached, so the real graph is picked up once the source recovers. `build_topology_provider(cfg)` selects on
`topology_source` (`static` default | `http`); `topology_url` is the endpoint. `TopologyMap` gained public
`services()` / `edges()` projections so the dashboard renders the graph through the SPI instead of reaching
into `_direct` (removed the `noqa: SLF001`).
**Why:** the hardcoded graph was the same lab-specific simplification VISION/ROADMAP P0.1 flag for telemetry
and actions — real correlation needs the live dependency graph from the operator's mesh/CMDB, and rooting an
incident on a stale or fabricated graph attributes the fault to the wrong service. **Invariants held:**
detection/correlation stay deterministic (#1) — the provider feeds the same code paths, never the LLM; a
down source degrades silently to the known-good fallback (#6, no fabricated graph, no spurious incident).
**Consequence:** 358 offline tests green (+12: provider parse/cache/degrade/recover, builder selection,
`edges`/`services`). Defaults keep the lab on the static literal, so the full suite ran untouched. The
remaining P0.1 seam is `ChangeFeed` (deploy/config/flag events from real systems).


## D-040 · 2026-06-14 · Multi-signal correlation engine — affinity graph over modalities, not a priority cascade (P1.1)
Closes the D-014 gap (the project's biggest design gap per ROADMAP P1.1). `Correlator.correlate` was a
3-step priority cascade — (1) dependency error codes → root, (2) topology `most_upstream`, (3) leftovers
stand alone — which is correct for clean error-code cascades but leaves ONE fault's *heterogeneous* signals
as separate tickets: a memory leak trips `crash_loop` AND `error_rate` on the same service (different
signal types, no shared error code); added latency trips `latency_p95` AND `error_rate`; the log / metric /
trace twins of one fault land separately. It was also single-modality (candidate↔candidate only) and
assumed near-simultaneity (a tight flush window). Rewrote it as an **affinity graph + connected components**
(union-find): one component = one incident, with edges across modalities — **E1** same service, **E2**
shared trace/request id, **E3** directional topology chain, **E4** dependency-error co-attribution.
Within a component, root precedence is: dep-error root (most precise) > a recently-changed service
(deploy/config coincidence, from the change log) > topology `most_upstream` > the member with the most
dependents (then earliest `first_seen`). Trace correlation — the strongest *causal* signal, already
gathered for diagnosis in `diagnosis/context.py` (keyed on `request_id`) — is promoted into correlation:
`trace_ids` was added to `Anomaly`/`IncidentCandidate`, populated by the error-rate detector from each
failing request's id and carried through the engine like `error_codes`. A shared trace id (E2) fuses
across services even when topology and error codes can't (sibling services over a shared dep) and **bypasses
the time window** (causal regardless of timing); the non-causal edges (E1/E3/E4) are gated by an adaptive
window (`correlation_max_span_s`, default 300s — real cascades propagate over minutes, not the 90s flush
window). The manager feeds recent non-agent change-log entries (`change_coincidence_window_s`, default 600s)
into `correlate`.
**Why:** the D-014 break was observed live (`loadgen:latency_p95` hard-escalating; multi-signal faults
producing multiple tickets). Error-code-keyed merging only works when a fault announces itself with a
dependency code; a leak/latency fault does not. Trace ids are the strongest causal link an SRE has, and we
were already collecting them downstream — not using them in correlation was the single biggest intellectual
gap. **Invariants held:** correlation stays pure deterministic code (#1 — no LLM); one fault = one ticket
is *strengthened* (#3); D-016 is preserved exactly — candidates that *merely share a dependency* still do
NOT merge (E3 requires a *directional* upstream/downstream chain between the two candidates' own services,
not a shared leaf; worker+gateway both depend on redis/postgres but neither is upstream of the other, so
they fuse only via an actual `redis_unreachable`/`db_unreachable` error code, which is E4). `stream_blind`
(service `_stream`, depends on nothing, no codes/traces) still stands alone — an agent-health signal, never
a lab incident (D-013). The earlier 3-step behavior is a strict subset of the new edges, so all prior
correlation tests pass unchanged.
**Consequence:** 368 offline tests green (+10: same-service heterogeneous fusion, latency/metric/trace
modality collapse, shared-trace cross-service fusion, window adaptivity, deploy-coincidence root, dep-error
precedence, no-merge-on-change, manager-level multi-signal one-ticket). New config: `correlation_max_span_s`,
`change_coincidence_window_s`. **Follow-ups:** (1) the metric/trace detectors don't yet populate `trace_ids`
(they fuse via same-service/topology); promoting Tempo `trace_id`s into those Anomalies would extend E2 to
the pure-metric path. (2) Add live `latency`/`memleak` eval scenarios scored by primary signal now that the
engine collapses them (D-014's deferred follow-up) — needs the lab up to validate end-to-end.


## D-041 · 2026-06-14 · State & HA — Postgres-backed shared stores + leader election (Maturity 9 / P1.2)
The agent's control plane (incident state, tickets, change log, post-mortems, the restart-cap
ledger, leader lock) was per-host SQLite in `.state/`: restart-safe on one host (D-009) but a
single point of failure — one process, one host, single-writer files you can't point two replicas
at, and no coordination primitive to stop two agents double-acting one incident. Closed it the way
the persistence-interface discipline (D-006/D-009) intended — *swap the backing store + add
coordination, not a redesign*: `state_backend` (config) selects `sqlite` (default, unchanged) or
`postgres` (a shared managed store) for ALL stores at once. New Postgres twins implement the exact
same interfaces — `PostgresIncidentStore`, `PostgresTicketStore`, `PostgresPostMortemStore`,
`PostgresChangeLog` — reusing each SQLite store's pure row↔model mapper verbatim, so the two
backends can't drift; the manager, the Composite Jira/Confluence stores, and the diagnosis read
paths are untouched. `PostgresRateLimiter` is the fleet-wide swap promised in D-038: the per-service
restart cap and the #5 change-log tag are one transaction serialised by a transaction-scoped
advisory lock keyed on the target, so the cap holds across replicas (a 20-thread test confirms
exactly `cap` succeed). Coordination is **leader election** (one acts, others hot-standby):
`PostgresLeadership` uses a *session-scoped* `pg_try_advisory_lock`, whose auto-release on
connection drop gives crash failover with no lease timer — only the leader runs detect→correlate→
ticket→diagnose→act→verify; standbys keep tailing/polling so their window stays warm and, because
state is shared, take over mid-incident without re-acting (D-009 at fleet scale). `SingleNodeLeadership`
is the no-op default so the single-process path is byte-identical. A stdlib `HealthServer` serves
`/healthz` (a dead-man's switch — liveness fails if the tick loop hasn't run in ~6 ticks, so k8s
restarts a wedged agent) and `/readyz` (leader-aware), extensible for the Maturity-11 `/metrics`.
`psycopg` is an optional extra (`pip install 'sre-agent[postgres]'`); the agent's state DB is
deliberately a SEPARATE database from any monitored system's data of record (invariant #4 — this is
the agent's own control plane).
**Why:** an SRE agent being down during an incident is the worst possible time; restart-safe isn't
failure-safe. Leader election is the minimum coordination that prevents a double-restart, and a
shared store is its precondition.
**Verified live (Docker up):** 376 offline tests green (+8 new) with `state_backend=sqlite`
unchanged; with `$SRE_STATE_DSN` set against a dedicated `sre-statedb` Postgres, 14 store/HA
contract tests pass (all four stores behave identically to SQLite; 20-thread cap holds at exactly
3). Two replicas against the shared store + live lab: exactly one LEADER, one standby (`/readyz`
503); killed the leader → standby took over (`/readyz` 200) — the P1.2 failover exit criterion. A
single `--postgres` agent against the live lab took the `errors` cascade end-to-end — the 4-service
fan-out collapsed to ONE `api:internal_error` incident persisted in Postgres, ticketed, diagnosed,
acted (dry-run), and verifying. **Consequence:** new config `state_backend`/`state_dsn(_env)`,
`ha_enabled`, `replica_id`, `leader_lock_key`, `health_enabled`/`health_port`; CLI `--postgres`,
`--ha`, `--health`. The dashboard (D-025) still reads the SQLite files; pointing it at a Postgres
projection is a follow-up (it's a demo read-only view, not on the agent's HA path). The multi-host
guardrail_store note in D-038 is now realized.


## D-042 · 2026-06-15 · Meta-monitoring — the agent watches itself (Maturity 11 / P1.3)
The agent's only self-signal was a stderr heartbeat: not alertable, no quantified false-positive
rate (invariant #6 was asserted by the null test, never measured continuously), no self-SLOs. If
the agent silently wedged — stuck loop, provider down past the cap, ingestion stalled — nobody
found out. Added a dependency-free Prometheus exposition (`sre_agent/metrics.py`: Counter / Gauge /
Histogram + `render()`, no prometheus_client needed) surfaced on the Maturity-9 health server's
`/metrics`. `AgentMetrics` is the semantic facade; the manager records lifecycle events
(`sre_detection_latency_seconds` from symptom first-seen to incident, `sre_mttr_seconds`,
`sre_escalations_total`, `sre_incident_outcomes_total`, `sre_actions_total{result}`,
`sre_diagnosis_latency_seconds`, `sre_diagnoses_total`), and the main loop stamps
`sre_last_tick_timestamp_seconds` (the dead-man's switch) + `sre_leader`/`sre_up` every tick. A
`NullMetrics` no-op keeps the manager API identical when metrics are off, so the offline suite is
untouched. **stream_blind is now correctly routed (closing a D-013 gap):** the manager intercepts a
`stream_blind` candidate in `ingest()` and fires an agent-health page + `sre_stream_blind_total`
instead of buffering/correlating/ticketing it — the agent going blind is an agent-health event, not
a lab incident. Shipped the SLO alerting as code (`deploy/alerts.yml`): the loud dead-man's-switch
(`time() - sre_last_tick_timestamp > 60`), scrape-failure, agent-blind, no-leader pages, plus
escalation-rate / MTTR-regression / false-positive-proxy tickets — routed through the same on-call
the agent uses for lab incidents. `deploy/prometheus-scrape.yml` and the HA `deploy/k8s/agent-
deployment.yaml` (2 replicas, liveness=/healthz, readiness=/readyz, Prometheus scrape annotations)
tie probes + metrics into k8s self-healing. This is also what gates the shadow-mode rollout (VISION
§5): shadow mode is only useful if FP rate and accuracy are measured, which now they can be.
**Why:** "you can't improve what you don't track" — invariant #6 demands a measured FP rate, and a
watcher that can go silently dark is the failure this project exists to prevent. The agent emitting
its own metrics also makes it a monitored production service like any other (VISION §6).
**Verified live (Docker up):** 383 offline tests green (+7: exposition primitives, AgentMetrics
recorders, the /metrics endpoint, manager lifecycle wiring, stream_blind-is-a-page-not-a-ticket). A
live `--postgres --health` agent against the lab served `/healthz` (alive), `/readyz` (ready), and
`/metrics` with `sre_up=1`, `sre_leader=1`, and an advancing `sre_last_tick_timestamp_seconds`.
**Consequence:** new config `health_enabled`/`health_port` (shared with Maturity 9); `--health` CLI;
new `deploy/` artifacts. The FP-rate alert is a proxy (incidents without a successful action) until
labelled operator feedback ("not a real incident" closures) exists — a P3 follow-up. Diagnosis
*cost* (tokens/$) isn't tracked yet — latency is; cost needs the provider to return usage.


## D-043 · 2026-06-15 · approve/reject CLI forces UTF-8 stdout (found in live end-to-end testing)
The operator approval CLI (`sre_agent/approve.py`) did not reconfigure its streams to UTF-8 the
way `main.py` does. On a default Windows console (cp1252), `reject` (and any path) drove
`_escalate` → `ConsoleNotifier`, which prints a line containing `→`; encoding that raised
`UnicodeEncodeError` **before** `IncidentStore.save`, so the escalation was silently lost — the
incident stayed `AWAITING_APPROVAL` while the CLI reported a crash. Fixed by mirroring main.py's
UTF-8 reconfigure block at CLI start and using ASCII arrows in the CLI's own prints.
**Why it mattered / how it was found:** surfaced only by driving the *real* CLI through a live
incident on a Windows console during end-to-end verification — no unit test caught it because
pytest's stdout isn't a cp1252 console. It's a correctness bug (a human rejecting a Tier-2 action
would think it failed, and the action would remain pending), exactly the class of issue the
"actually run it" pass exists to catch. **Guard:** `tests/test_approve_cli.py` reproduces it in a
subprocess with `PYTHONIOENCODING=cp1252` and asserts the CLI exits 0 and persists ESCALATED.
**Consequence:** 384 offline tests green (+1). The notifier still prints `→` for humans; the fix is
that the CLI's console can always encode it.
