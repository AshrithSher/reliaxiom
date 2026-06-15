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
