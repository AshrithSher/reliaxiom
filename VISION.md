# VISION.md — from lab agent to a universal incident-response platform

> **Where we are:** a fully working autonomous SRE agent, validated end-to-end on the 7→9
> container lab (`logs-streaming-demo-app`). **Where we are going:** the *same engine*, plugged
> into **any application, in any cloud or on-prem**, through swappable adapters with **MCP as the
> primary connector**.

This document is the north star. It does not change any invariant in
[CLAUDE.md](CLAUDE.md) or [AGENT.md](AGENT.md) — it explains how the proven core generalizes.
Build order and priority live in [ROADMAP.md](ROADMAP.md) (Part II); rationale in
[DECISIONS.md](DECISIONS.md).

---

## 1. The positioning

**"Cheap code watches everything; the LLM wakes only when something is wrong; humans approve
anything risky"** — and that loop should run against *your* system, wherever it lives, without
us re-writing the agent for your stack.

The agent is valuable precisely because its **core is substrate-agnostic**: detection,
correlation, the incident state machine, tiered actions, diagnosis, HITL, post-mortems, and the
trust guards are all pure logic over abstract objects (`LogRecord`, `IncidentCandidate`,
`Incident`, `Diagnosis`, `ActionResult`). Everything that is *specific to an environment* —
where logs come from, how you restart a thing, what your dependency graph is — is an **adapter**.

> **The whole platform thesis in one line:** the engine never learns what cloud you run on; the
> adapters do, and most adapters are just **MCP servers**.

---

## 2. The universality model — three rings

```
        ┌───────────────────────────────────────────────────────────┐
        │  RING 0 — THE ENGINE (substrate-agnostic, already built)   │
        │  detect · correlate · state machine · diagnose · tier ·    │
        │  guardrails · verify · HITL · post-mortem · incident memory│
        └───────────────▲───────────────────────────▲───────────────┘
                        │ Provider SPIs (interfaces) │
        ┌───────────────┴───────────────────────────┴───────────────┐
        │  RING 1 — ADAPTERS (per-environment, swappable)            │
        │  TelemetrySource · ActionBackend · TopologyProvider ·      │
        │  ChangeFeed · TicketStore · Notifier · PostMortemStore     │
        └───────────────▲───────────────────────────▲───────────────┘
                        │     mostly spoken over     │
        ┌───────────────┴───────────────────────────┴───────────────┐
        │  RING 2 — CONNECTORS (the wire to the real world)          │
        │  ▸ MCP servers (primary): Prometheus, Loki, Datadog,       │
        │    Grafana, CloudWatch, Elastic, Kubernetes, AWS/GCP/Azure,│
        │    Terraform, PagerDuty/Opsgenie, Jira/ServiceNow, Slack…  │
        │  ▸ Native SDK/REST adapters (where no MCP server exists)   │
        └───────────────────────────────────────────────────────────┘
```

Ring 0 exists today. Ring 1 is **mostly** built — ticketing, notifications, and post-mortems
are interface-backed with stub + real implementations (D-006, D-024); the pollers inject their IO
(D-015); and the four formerly-coupled seams are now SPIs: **TelemetrySource** (D-032, metrics/
logs/traces), **ActionBackend** (D-037, Docker + Kubernetes), **TopologyProvider** (D-039), with
only **ChangeFeed** remaining. Beyond Ring 1, the engine now also runs **HA** on shared Postgres
with leader election (D-041) and **monitors itself** with Prometheus metrics, health probes, and a
dead-man's switch (D-042). The remaining work — the **ChangeFeed** seam and the **Ring-2 MCP
layer** — is in [ROADMAP.md](ROADMAP.md) Part II.

---

## 3. Why MCP is the connector of choice

To "connect to any application in any cloud or on-prem" we need a connector model that is
**open, discoverable, credential-scoped, and already proliferating** across the exact systems an
SRE agent must touch. MCP is that substrate.

**The agent as an MCP _client_ (consume the world).** Point it at MCP servers for:

| Need (Ring-1 SPI) | MCP servers it can speak to |
|---|---|
| `TelemetrySource` (read) | Prometheus, Loki, Grafana, Datadog, CloudWatch, Elastic/OpenSearch |
| `ActionBackend` (write) | Kubernetes, AWS, GCP, Azure, Terraform, Argo/Flux, CI/CD |
| `TopologyProvider` (read) | service-mesh / trace-graph / CMDB / Backstage catalog |
| `ChangeFeed` (read) | deploy systems, GitOps, feature-flag platforms |
| `TicketStore` / `Notifier` | Jira, ServiceNow, PagerDuty, Opsgenie, Slack, Teams |

**The agent as an MCP _server_ (be part of an agent mesh).** Expose read tools and gated write
tools so other agents, copilots, or a human in their IDE/chat can: query live incident state,
pull a post-mortem, subscribe to incident events, and **request/approve a Tier-2 action** — the
same approval path the dashboard and CLI already use.

**Connectivity tiers (the extensibility promise):**
1. A native MCP server exists → **just configure it** (zero code).
2. No MCP server → write a thin **SPI adapter** (or a small MCP shim) — bounded, testable work.
3. Either way the engine is untouched.

---

## 4. What MUST stay invariant when actions arrive over MCP

MCP makes the agent *more* capable and *more* connected, which makes the safety rails **more**
important, not less. The non-negotiables from CLAUDE.md extend verbatim:

- **MCP tools are transport, never policy.** A discovered MCP tool does **not** become an action
  the LLM can call directly. It is mapped into the **enumerated action catalog** with an
  **operator-assigned tier** (auto / approval / escalate). Unmapped tools are inert — the model
  cannot select them (invariant #2; D-003). A confident-but-wrong model still cannot run a
  risky MCP tool.
- **No LLM in the detection loop** — telemetry MCP servers feed deterministic detectors only
  (invariant #1).
- **Every MCP action is tagged in the change log before execution**, so detection ignores the
  transient it causes (invariant #5/#7).
- **Never destructive to data of record** — no MCP tool, in any tier, performs a destructive data
  operation (invariant #4); this is enforced at the catalog mapping, not trusted to the server.
- **Prompt-injection is now an attack surface:** logs/metrics pulled via MCP are *untrusted
  input* to diagnosis. The evidence-grounding and double-run guards (D-004/D-018) are the
  backstop; tiers-in-code is the hard stop.

---

## 5. The rollout discipline (how anyone safely adopts it)

Universality is meaningless without trust. Every new environment onboards through the same
trust tiers, gated by measured quality (see meta-monitoring, ROADMAP P1):

1. **Shadow** — detect + diagnose + write tickets; **execute nothing**. Measure false-positive
   rate and root-cause accuracy on real incidents. (`--execute` off; already supported, D-019.)
2. **Suggest** — every action becomes a Tier-2 proposal a human approves.
3. **Auto (whitelisted)** — enable Tier-1 auto-remediation for a few proven, low-blast-radius
   fingerprints; expand as confidence accrues. Incident memory (D-022) makes the agent *more*
   autonomous as it sees a fault repeat.

A **global kill switch** and **fleet-wide action rate limits** precede tier 3 in any
environment.

---

## 6. North-star definition of done

> An operator connects the agent to their stack by listing a handful of **MCP servers** (or
> writing one thin adapter), runs it in **shadow** for a week, reviews a **false-positive /
> MTTR dashboard**, then graduates fingerprints to **auto** — all without forking the engine.
> The agent runs **HA** on their cluster, keeps state in **their** datastore, authenticates
> operators through **their** SSO, and is itself **monitored** like any other production service.

Everything in [ROADMAP.md](ROADMAP.md) Part II is ordered to reach that sentence.
