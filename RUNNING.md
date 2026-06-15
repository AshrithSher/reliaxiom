# RUNNING.md — demo & operations guide

How to **run the SRE agent and show it to a client**. It is organised as a story:

1. **What you're showing** — the one-paragraph pitch and the loop.
2. **The surfaces** — every screen, what's on it, what to point at.
3. **Setup** (once) → **Start a session** (each time).
4. **The demo flow** — a ~7-minute narrative, click by click, with *what to say*.
5. **Every scenario** — the full fault menu as a reference table.
6. **"This is production-grade, not a toy"** — HA, self-monitoring, Kubernetes, safety proofs.
7. **Command reference** — what you get when you run each command.
8. **Reset / teardown / gotchas.**

---

## 1. What you're showing

> **Cheap code watches everything, always. The LLM wakes up only when something is actually
> broken. The agent fixes what's safe to fix itself, asks a human before touching anything
> stateful, and escalates when it's unsure — and it does all of this on your real stack, with
> real tickets and real post-mortems.**

The loop, every incident:

```
detect (no LLM)  →  correlate (one fault = one ticket)  →  diagnose (LLM)  →
   tier (hard-coded)  →  act / ask a human / escalate  →  verify recovery  →  resolve  →  post-mortem
```

The three things that make it credible: **(a)** detection is deterministic code, so it's silent on
a healthy system; **(b)** the *risk tier* of every action is fixed in code, never chosen by the
model; **(c)** it verifies recovery on the same signal it detected on, then writes the post-mortem.

---

## 2. The surfaces (what each screen is, what to highlight)

| Screen | URL | What it is | Point at… |
|---|---|---|---|
| **Agent console** | http://localhost:8000 | the demo cockpit — start/stop the agent, inject faults, approve, watch KPIs | topology graph, incident feed, incident drawer, KPI strip |
| **Grafana** | http://localhost:3000 | the agent's "eyesight": metrics + logs + traces | Explore → Prometheus / Loki / Tempo |
| **Prometheus** | http://localhost:9090/graph | raw PromQL | the 5xx-ratio and p95 queries the agent detects on |
| **Jira (HELP)** | your Atlassian site | the real ticket each incident opens | lifecycle comments, severity→priority, fingerprint label |
| **Confluence (ReliAxiom)** | your Atlassian site | the post-mortem each resolved incident publishes | timeline, root cause, MTTx |
| **Agent /metrics** | http://localhost:9108/metrics | the agent monitoring *itself* (Part 6) | `sre_mttr_seconds`, `sre_last_tick…` (dead-man's switch) |

**The Agent console in detail — the four things on screen:**
- **Service topology** — the dependency graph. Healthy nodes are green; an affected service turns
  red and goes back to green when the agent fixes it.
- **Incident feed** — one row per incident, walking its lifecycle live:
  `detected → diagnosing → acting → verifying → resolved` (or `awaiting_approval` / `escalated`).
- **Incident drawer** (click a row) — the **LLM root-cause text**, the **evidence it cited**, the
  **action it took**, the **tier**, and a **Jira ↗** link to the real ticket.
- **KPI strip** — `auto-resolve %`, `pages avoided`, `MTTR`, `fastest resolve`, incident counts.
  These are the business numbers, computed from the post-mortem archive.

---

## PART 0 — ONE-TIME SETUP (do once per machine)

```powershell
cd C:\Mine\Concave\SRE
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,dashboard]"        # add ,postgres for the HA path (Part 6)
```

**Secrets** (gitignored — create `.secrets\`):
- `.secrets\llm.env` — AI diagnosis. At least one key; both = automatic failover.
  ```
  OPENROUTER_API_KEY=...
  GEMINI_API_KEY=...
  ```
- `.secrets\atlassian.env` — *optional*, only for real Jira/Confluence. Skip it and the agent uses
  its built-in local stores (tickets/post-mortems still work, just no Atlassian).
  ```
  ATLASSIAN_SITE=https://your-site.atlassian.net
  ATLASSIAN_EMAIL=you@example.com
  ATLASSIAN_API_TOKEN=...
  JIRA_PROJECT_KEY=HELP
  CONFLUENCE_SPACE_KEY=ReliAxiom
  ```

**Sanity check:** `python -m pytest -q` → **`384 passed, 10 skipped`** (the skips are live-Kubernetes
and Postgres-gated tests — Parts 6).

---

## PART 1 — START A SESSION (each time)

1. **Docker Desktop running?** The whale icon 🐳 is steady; `docker ps` lists containers.
2. **Bring it all up — one command:**
   ```powershell
   .\scripts\demo-up.ps1            # add -Reset to wipe prior incident history first
   ```
   **What you get:** all **16 containers** (the 9-service app + Prometheus/Loki/Tempo/Grafana/…)
   start, the **dashboard** launches on :8000, and your browser opens to it.
3. **Confirm the app is healthy:** `curl http://localhost:8080/products` → HTTP 200 with JSON.
   *(A 502 = the gateway came up before its upstreams; `docker compose ... restart gateway`.)*
4. **Open the tabs:** Agent console (:8000), Grafana (:3000), Prometheus (:9090).

> **For a client demo, start clean:** `.\scripts\demo-up.ps1 -Reset` so the KPI strip and incident
> feed start empty and every number you show was earned during the demo.

---

## PART 2 — THE DEMO FLOW (≈7 minutes, click by click)

Run it in this order — it builds from "it's quiet" to "it fixes things" to "it knows when to ask."

### Step 0 — Start the agent (and show it's silent)
- **Click:** **Start agent** (top-left).
- **What happens:** the agent launches live (`--execute --jira --confluence`) and begins watching
  logs, metrics, and traces.
- **Show:** the **Agent log** heartbeat — `# [..] healthy · N lines · 9 services · containers: all up`.
  The topology is all green; the incident feed is empty.
- **Say:** *"It's watching everything right now — and saying nothing, because nothing is wrong.
  Silence on a healthy system is the whole point; a noisy monitor gets ignored."*

### Step 1 — Auto-fix (the headline) · **API 500s**
- **Click:** **API 500s**.
- **What the agent does:** detects an error spike, **collapses the api→webapp→gateway→loadgen
  cascade into ONE incident**, the LLM diagnoses it, it **restarts api (Tier-1, automatic)**, then
  verifies the 5xx ratio dropped before resolving.
- **Show:** topology — api goes red then green; incident feed walks `detecting → diagnosing →
  acting → verifying → resolved`; open the drawer for the **root cause + evidence + Jira ↗**.
- **Say:** *"Four services screamed; it filed **one** ticket, found the root, fixed it, and proved
  recovery — no human touched it."*

### Step 2 — Correlation at the root · **Auth tier failing**
- **Click:** **Auth tier failing.**
- **What the agent does:** auth 5xx fans out to api/webapp/gateway/loadgen; correlation roots the
  whole thing at **auth** and restarts **auth**, not the symptoms.
- **Show:** several nodes red, but **one** incident `auth:unreachable`; the drawer names auth as root.
- **Say:** *"It fixed the cause, not the five places that hurt."*

### Step 3 — The silent fault · **Worker stopped**
- **Click:** **Worker stopped.**
- **What the agent does:** no errors anywhere — the worker just goes quiet. The silence detector
  catches it and **restarts worker (Tier-1)**.
- **Say:** *"The scariest outages are silent. Threshold-on-errors would miss this; absence-of-signal
  is itself a signal."*

### Step 4 — Ask a human first · **Postgres down** (Tier-2 approval)
- **Click:** **Postgres down.**
- **What the agent does:** roots it at the **stateful** postgres → **does not act**. It posts a
  proposal with **blast radius** and enters `AWAITING_APPROVAL`.
- **Show:** the drawer now has **Approve / Reject** and the blast radius.
  - **Approve** → it restarts postgres, verifies, resolves; *your name is recorded on the ticket*.
  - **Reject** → it escalates with **no action**, assigned to on-call.
  - **Do nothing** → after the timeout it escalates automatically.
- **Say:** *"Stateless things it fixes itself. Anything that could lose data, a human approves —
  and that rule lives in code, so a confident-but-wrong model can't override it."*

### Step 5 — Knowing when to stop · **escalation / the restart cap**
- **Click:** **API 500s** and let it auto-fix a few times in a row.
- **What the agent does:** after the **restart cap** (per service per hour) is hit, the next attempt
  is **blocked by a guardrail and the incident escalates** to a human.
- **Say:** *"It also knows when to stop trying and hand off — restart-looping a broken service is
  how automation makes outages worse."*

### Step 6 — Reset
- **Click:** **Reset lab** (clears chaos, restarts any stopped containers). Point at the **KPI
  strip** — *auto-resolve %, pages avoided, MTTR* — all earned in the last seven minutes.

---

## PART 3 — EVERY SCENARIO (reference)

All of these are dashboard buttons (or the terminal command shown). Reset between them.

| Button / command | Simulates | Agent response | Tier |
|---|---|---|---|
| **API 500s** | api returns ~70% 5xx | one `api:internal_error` incident → restart api → verify | **Auto (1)** |
| **API latency** | api adds 2–6 s | `metric_latency_p95` → restart api → verify p95 under threshold | **Auto (1)** |
| **Worker stopped** | the silent fault | `worker:silence` → restart worker → verify it logs again | **Auto (1)** |
| **Auth tier failing** | login cascade | fan-out collapses to one `auth:unreachable` → restart auth | **Auto (1)** |
| **Payments provider down** | charges fail | one `payments:unreachable` → restart payments | **Auto (1)** |
| **Postgres down** | database outage | roots at stateful postgres → **awaits approval** | **Approval (2)** |
| **Redis down** | cache outage | roots at stateful redis → **awaits approval** | **Approval (2)** |
| `…/chaos/memleak/start` (terminal) | OOM / crash-loop | repeated `startup` → `api:crash_loop` → restart api | **Auto (1)** |

Terminal chaos (if you prefer the CLI to the buttons):
```powershell
docker exec api      python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/on',method='POST'))"
docker exec api      python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/latency/on',method='POST'))"
docker stop worker    # / postgres / redis
docker exec api      python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/memleak/start',method='POST'))"
```

---

## PART 4 — THE AGENT'S THREE KINDS OF EYESIGHT (Grafana / Prometheus / Tempo)

This is where you *prove* the agent isn't guessing from logs alone — it detects on **real metrics**,
verifies on the **same metric**, and a **trace** shows exactly where a request broke. Everything the
agent fires on, you can watch fire in Grafana at the same moment.

### 4.0 — How to drive Grafana (do this once, keep it open)
1. Open **http://localhost:3000** → no login (anonymous is on for the lab).
2. Click the **compass icon → Explore** (top-left).
3. **Pick the datasource** in the dropdown at the very top-left: **Prometheus**, **Loki**, or **Tempo**.
4. Paste a query (below), set the time range to **Last 15 minutes** (top-right), and click **Run query**
   (or the blue **Run** ▶). For metrics, leave it on the **Graph** view; turn on **auto-refresh (5s)**
   so the line moves live as you inject a fault.

> Prefer raw Prometheus? Same queries paste into **http://localhost:9090/graph** → Execute → **Graph** tab.

### 4.1 — The four standing panels (keep these on screen during the whole demo)
Open four Explore tabs (or four browser tabs), one query each:
```promql
# P1 — request rate per service (the "is traffic flowing" panel)
sum by (service) (rate(http_requests_total[1m]))

# P2 — 5xx ERROR RATIO per service        → the agent's error detector (fires at 20%)
sum by (service) (rate(http_requests_total{status=~"5.."}[1m]))
  / sum by (service) (rate(http_requests_total[1m]))

# P3 — real p95 LATENCY in ms              → the agent's latency detector (fires at 1000ms)
histogram_quantile(0.95, sum by (service,le) (rate(http_request_duration_seconds_bucket[1m]))) * 1000

# P4 — worker job-queue BACKLOG + throughput → the agent's saturation detector
queue_depth
rate(jobs_processed_total[1m])
```
On a healthy system: P1 is steady, **P2 sits at 0**, P3 is a flat ~30–60 ms, P4's `queue_depth` ≈ 0 and
`jobs_processed_total` ticks along. *That flatness is the story — point at it before you break anything.*

### 4.2 — What to show for each error (which panel moves, and what it means)

| Inject | Open this | What you'll see | What it means / say |
|---|---|---|---|
| **API 500s** | **P2** (5xx ratio) + Loki `{service="api"} \|= "error"` | api's line jumps 0 → ~0.7, then webapp/gateway/loadgen follow | "This is the exact ratio the agent's error detector watches. Four lines moved — it filed **one** ticket." |
| **API latency** | **P3** (p95) + Tempo | api's p95 jumps ~50 ms → **2000–6000 ms**; P2 stays ~0 | "A **real histogram** p95, not a log guess — and it **verifies recovery on this same line** dropping back under 1 s." |
| **Worker stopped** | **P4** (queue) | `queue_depth` climbs steadily; `jobs_processed_total` rate → **0**; **P2 never moves** | "No errors anywhere — the work just silently stops. Absence of signal *is* the signal." |
| **Auth tier failing** | **P2** + Tempo trace | 5xx rises on **auth AND** api/webapp at the same time | "Symptoms in four places, one cause. The trace will prove the cause is auth." |
| **Payments down** | **P2** (payments) + **P4** | payments 5xx rises; worker can't charge → `queue_depth` grows | "The failure is downstream in payments; the worker is the victim, not the cause." |
| **Postgres down** | **P2** (api/webapp/worker) | a broad `db_unreachable` 5xx cascade | "Stateful outage — watch the agent root it at postgres and **ask for approval** instead of acting." |
| **Redis down** | **P2** + Loki `\|= "redis"` | `cache_degraded` then `redis_unreachable` | "Stateful again → Tier-2 approval." |
| **Memleak (terminal)** | `docker stats api` + Loki `\|= "memory_pressure"` | MEM marches to the 256 MiB cap → OOM → a fresh `startup` log | "Repeated `startup` lines = crash-loop; the agent restarts api." *(cAdvisor can't always label by container on Docker Desktop — use `docker stats` for the memory view.)* |

**The recovery half (show it for any auto-fix):** after the agent acts, the moved panel returns to
baseline — P2 back to 0, or P3 back under 1 s. That return is *exactly* what the agent's recovery
predicate checks before it writes "resolved." Point at the line crossing back under the threshold.

### 4.3 — Traces (Tempo): showing *where* a request broke
Explore → **Tempo** → **Search** tab → **Service Name** `api` → **Run** → click any trace → you get the
**`gateway → webapp → api → {postgres, redis, auth}`** waterfall. To jump straight to failures, use the
**TraceQL** tab: `{ status = error }`.
- **During Auth tier failing:** open a failing trace — the **red span is `auth`**, and its parent `api`
  span is errored *because* of it. That's visual, undeniable proof of root cause: *"the agent said auth;
  the trace shows auth."*
- **During API latency:** the **api span is visibly long** in the waterfall — you can *see* the 2–6 s.

### 4.4 — Logs (Loki): the raw evidence the agent cited
Explore → **Loki** → `{service="api"}` (add `|= "error"` to filter). When you open an incident's drawer
in the console, the **evidence it cited** (request ids, error codes) is in these same lines — the agent
isn't allowed to cite anything that isn't really here (hallucinated evidence is grounds for escalation).

### 4.5 — The agent monitoring *itself* (close the loop)
The lab metrics above are the agent's *eyes*. The agent also emits its *own* Prometheus metrics — run a
terminal agent with `--health` and scrape :9108 (see Part 5.5). In Prometheus you can then graph
`sre_mttr_seconds`, `sre_actions_total`, `rate(sre_candidates_total[5m])` next to the lab panels — the
watcher, watched.

---

## PART 5 — "THIS IS PRODUCTION-GRADE, NOT A TOY"

Show as many of these as the audience wants depth for.

### 5.1 Real Jira + real Confluence
Every incident is a Jira issue in **HELP** (severity→priority, lifecycle→workflow, fingerprint as a
dedup label, comments for diagnosis/action/approval/resolution). Every resolved/escalated incident
publishes a **Confluence post-mortem** to **ReliAxiom**. *Both keep a local mirror, so an Atlassian
outage never blocks the agent.*

### 5.2 Incident memory (it gets smarter)
Run the **same** fault **twice** without resetting state. The second incident's diagnosis text
**references the prior post-mortem** ("we've seen this; X fixed it").
```powershell
python -m sre_agent.report      # weekly-style report: volume, recurring faults, auto-resolve %
type .state\postmortems\INC-1.md
```

### 5.3 Remediation on real Kubernetes, under a locked-down identity
Proves the engine isn't tied to Docker/your laptop: it remediates a real Deployment on a real (local,
free) `kind` cluster, authenticating as a ServiceAccount that can do **exactly** `get/list/patch` on
Deployments in one namespace — nothing else. Four short beats:

**(a) One-time: create the cluster + apply the scoped permissions**
```powershell
.\scripts\setup-kind.ps1     # makes cluster "sre-lab" + applies deploy\k8s\rbac.yaml
```
- **Show:** the tail prints `serviceaccount/sre-agent created`, `role/rolebinding created`, and the API
  server URL + a token. *(First run downloads a ~1 GB node image — minutes; after that it's instant.)*

**(b) The security headline — let Kubernetes itself say what the agent may do**
```powershell
$sa = "system:serviceaccount:lab:sre-agent"
kubectl --context kind-sre-lab -n lab auth can-i patch  deployments --as=$sa   # yes
kubectl --context kind-sre-lab -n lab auth can-i delete deployments --as=$sa   # no
kubectl --context kind-sre-lab -n lab auth can-i get    secrets     --as=$sa   # no
kubectl --context kind-sre-lab -n lab auth can-i '*'    '*'         --as=$sa   # no
```
- **Show:** one **`yes`** (restart) and three **`no`**s. **Say:** *"The blast radius is an RBAC grant,
  not host-root. It can roll a Deployment and literally nothing else — it can't delete a workload or
  read a secret. That's the line a security team signs off on."* *(A `no` answer exits non-zero — that's
  the expected proof, not an error.)*

**(c) Put a service on the cluster and let the agent's real backend restart it**
```powershell
kubectl --context kind-sre-lab -n lab create deployment worker --image=nginx --replicas=2
kubectl --context kind-sre-lab -n lab rollout status deployment/worker --timeout=120s
kubectl --context kind-sre-lab -n lab get pods -l app=worker        # note the pod name hashes + AGE

# gather the scoped connection details and run the AGENT'S OWN KubernetesActionBackend:
$server = kubectl --context kind-sre-lab config view --minify -o jsonpath='{.clusters[0].cluster.server}'
$caData = kubectl --context kind-sre-lab config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}'
$caFile = Join-Path (Get-Location) ".state\kind-ca.crt"
[IO.File]::WriteAllBytes($caFile, [Convert]::FromBase64String($caData))
$env:SRE_KUBE_API_SERVER=$server
$env:SRE_KUBE_TOKEN=(kubectl --context kind-sre-lab -n lab create token sre-agent --duration=1h)
$env:SRE_KUBE_CA=$caFile
python scripts\k8s_restart_demo.py
```
- **Show:** the three lines it prints —
  `BEFORE: ready 2/2, gen 1/1` → `AGENT ACTION: rollout restart issued (HTTP 200)` →
  `AFTER: ready 2/2, gen 1/2`. **Say:** *"`gen 1 → 2` means it's an idempotent declarative rollout —
  the same `kubectl rollout restart` mechanism — and it verifies against the Deployment's
  `readyReplicas`/`observedGeneration`, **the cluster's real state**, not a CLI exit code."*

**(d) Prove the pods were actually replaced**
```powershell
kubectl --context kind-sre-lab -n lab rollout status deployment/worker --timeout=120s
kubectl --context kind-sre-lab -n lab get pods -l app=worker
```
- **Show:** the pod-name hashes **changed** and **AGE reset** — the agent replaced the running workload
  on a real cluster, through a least-privilege identity. **Say:** *"Same engine, same decision logic —
  only the remediation backend swapped from Docker to Kubernetes. Detection, tiers, and approvals are
  untouched."* (Backend code: `sre_agent\action\k8s_backend.py`; RBAC: `deploy\k8s\rbac.yaml`.)

### 5.4 High availability — no single point of failure
State moves to shared Postgres; replicas elect a leader; only the leader acts; kill it and a standby
takes over **without re-acting**.
```powershell
docker run -d --name sre-statedb -e POSTGRES_PASSWORD=sre -e POSTGRES_DB=sre -e POSTGRES_USER=sre -p 5433:5432 postgres:16
$env:SRE_STATE_DSN = "host=localhost port=5433 dbname=sre user=sre password=sre"
python -m pytest tests/test_pg_stores.py tests/test_ha_coordination.py -q     # 14 passed
# then run two replicas with --postgres --ha --health (health_port 9108 / 9109); stop the leader,
# watch the standby's /readyz flip 503 → 200 as it takes over.
```
- **Say:** *"An SRE agent being down during an incident is the worst possible time — so it runs HA,
  and the restart cap is atomic across the whole fleet."* (Manifest: `deploy\k8s\agent-deployment.yaml`.)

### 5.5 It monitors itself (watch the watcher)
```powershell
python -m sre_agent.main --health --config configs\fast-demo.json
curl http://localhost:9108/metrics    # sre_mttr_seconds, sre_escalations_total, sre_actions_total…
curl http://localhost:9108/healthz    # liveness — a dead-man's switch if the loop wedges
curl http://localhost:9108/readyz     # readiness — leader-aware
```
- **Say:** *"It emits its own Prometheus metrics — detection latency, MTTR, escalation rate — and
  pages on a stalled heartbeat or if it goes blind. It's watched like any production service."*
  SLO alert rules + scrape config are in `deploy\alerts.yml` and `deploy\prometheus-scrape.yml`.

### 5.6 Safety proofs (one command each)
```powershell
python -m pytest tests/test_ratelimit.py::test_concurrent_consumers_never_exceed_cap -v   # cap holds under 20 racers
python -m eval.harness --null --duration 900                                              # 15 min healthy → ZERO incidents
```
- **Say:** *"False positives are treated as the worst kind of bug. The null test is the headline
  guarantee: an hour of healthy traffic produces total silence."*

### 5.7 More beats to show if the audience wants depth
- **It doesn't alarm on its own fix (self-suppression).** When the agent restarts a service, that
  restart looks like a crash to a naive monitor. Watch the incident feed during any auto-fix: there is
  **never a second incident** for the agent's own restart. Inspect the proof — every agent action is
  tagged in the change log *before* it runs, and detection ignores anomalies inside that tag's window:
  ```powershell
  python -c "import sqlite3; c=sqlite3.connect(r'.state\changes.db'); c.row_factory=sqlite3.Row; [print('-',r['actor'],r['change_type'],r['service']) for r in c.execute('select actor,change_type,service from changes')]"
  ```
  *(One `sre-agent restart_container <svc>` row per fix — the tag that stops it diagnosing itself.)*
- **Dedup (one fault = one ticket, not a storm).** Leave a fault on without resetting. The incident
  feed shows **one** incident gaining **comments**, not a new ticket every cycle. *"A flapping service
  pages you once, then updates the same ticket."*
- **Maintenance mode (planned-work silence).** Run a fault drill with **zero** tickets/alerts:
  ```powershell
  python -m sre_agent.main --config configs\fast-demo.json --maintenance
  ```
  *"During a deploy or a game-day, you don't want the agent paging — it still detects and diagnoses,
  but files nothing."*
- **It gets smarter (incident memory).** (Part 5.2) The **second** occurrence of a fault cites the
  prior post-mortem in its diagnosis — *"we've seen this; here's what fixed it last time."*

---

## PART 6 — COMMAND REFERENCE (what you get when you run it)

| Command | What you get |
|---|---|
| `.\scripts\demo-up.ps1 [-Reset]` | lab + observability stack + dashboard + browser open on :8000 |
| **Dashboard → Start agent** | live agent (`--execute --jira --confluence`): detects, diagnoses, fixes, asks, escalates |
| `python -m sre_agent.main --config configs\fast-demo.json` | terminal agent, **dry-run** (decisions only, restarts nothing) |
| `…\main.py … --execute` | same, but it **actually** restarts containers |
| `…\main.py … --maintenance` | detect + diagnose but file **zero** tickets/alerts (fault drills) |
| `…\main.py … --postgres --ha --health` | HA replica on shared Postgres + health/metrics server |
| `python -m sre_agent.approve list` | incidents awaiting human approval |
| `python -m sre_agent.approve approve INC-3 --by you --execute` | approve a Tier-2 action → it runs, verifies, resolves |
| `python -m sre_agent.approve reject INC-3 --by you` | reject → the incident escalates, no action |
| `python -m sre_agent.report` | weekly report: volume, noisiest services, recurring faults, auto-resolve % |
| `python -m eval.harness --scenario api-errors` | score one labeled fault (detected? time-to-detect?) |
| `python -m eval.harness --all` | score every scenario in sequence |
| `python -m eval.harness --null --duration 900` | the silence guarantee — must produce zero incidents |
| `python -m sre_agent.dashboard` (or via demo-up) | the read-only console on :8000 |

Config files: `configs\demo.json` (dashboard default — metrics + traces on),
`configs\fast-demo.json` (short timings for terminal scenarios).

---

## RESET / TEARDOWN / GOTCHAS

**Reset between faults:** dashboard **Reset lab**, or `.\scripts\demo-up.ps1 -Reset` for a fully
clean slate (wipes incident history too).

**Teardown:**
```powershell
.\scripts\demo-down.ps1                 # stop the lab + dashboard ( -Reset wipes state, -Hard removes the lab )
docker rm -f sre-statedb                # only if you started the HA state DB (Part 5.4)
kind delete cluster --name sre-lab      # only if you created the Kubernetes cluster (Part 5.3)
```

**Gotchas (these bite):**
1. **Docker must be running** before anything else.
2. **Reset between faults** — otherwise a new fault dedups onto the previous incident and "nothing
   seems to happen."
3. **One agent at a time on the default (SQLite) path** — don't run the dashboard agent *and* a
   terminal agent *and* the harness together. (The **HA path**, Part 5.4, is the deliberate
   exception: multiple replicas coexist and leader election keeps only one acting.)
4. **`--execute` actually restarts containers.** Omit it (dry-run) to show decisions only.
5. **LLM quota:** a `429` / `undetermined` escalation means the key is rate-limited — having both
   OpenRouter and Gemini keys lets it fail over automatically.

---

## WHERE THE CODE LIVES (the integration map)

| Layer | Path | Start here |
|---|---|---|
| Data objects | `sre_agent/models.py` | `IncidentCandidate`, `LogRecord`, `Trace` |
| Ingestion (logs) | `sre_agent/ingest/` | `tailer.py` → `parser.py` → `window.py` |
| Telemetry (metrics/logs/traces) | `sre_agent/telemetry/` | `sources.py` (SPIs), `adapters.py` |
| Detection | `sre_agent/detect/` | `engine.py` (debounce/cooldown), then each detector |
| Incidents | `sre_agent/incident/` | `manager.py`, `correlation.py` (multi-signal), `topology.py`, `pg_store.py` |
| Diagnosis (LLM) | `sre_agent/diagnosis/` | `diagnoser.py`, `context.py`, `llm.py` |
| Actions | `sre_agent/action/` | `catalog.py` (tiers), `backend.py` + `k8s_backend.py`, `ratelimit.py` + `pg_ratelimit.py` |
| Integrations | `sre_agent/integrations/` | `ticketing.py` + `pg_ticketing.py`, `confluence.py`, `postmortems.py` + `pg_postmortems.py` |
| State & HA | `sre_agent/` | `state_factory.py`, `pgdb.py`, `ha/leader.py` |
| Self-monitoring | `sre_agent/` | `health.py`, `metrics.py` |
| Dashboard | `sre_agent/dashboard/` | `server.py`, `control.py`, `metrics.py` |
| Wiring | `sre_agent/main.py` + `approve.py` | `build_engine()`, `manager.step()`, the factories |
| Deploy | `deploy/` + `scripts/` | `k8s/rbac.yaml`, `k8s/agent-deployment.yaml`, `alerts.yml` |

Design rationale for every choice is in **DECISIONS.md** (D-001…D-043).
