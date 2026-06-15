# DEMO.md — running the agent with metrics, logs & traces

A per-fault script: what to **inject**, what to **show** (PromQL / Grafana / traces / agent),
and whether the agent **auto-fixes or escalates** and why. Everything here is free and local.

## 0. One-time setup

```powershell
python -m sre_agent.main --config configs/demo.json --execute
# bring up the lab + the free Grafana LGTM stack (Prometheus/Loki/Tempo/Grafana)
cd C:\Mine\Concave\logs-streaming-demo-app
docker compose up -d --build
# sanity: a request should return 200 (if you see 502, run: docker compose restart gateway)
curl http://localhost:8080/products
```

Open these tabs:
| Surface | URL | What it shows |
|---|---|---|
| **Agent UI** | http://localhost:8000 | incidents, lifecycle, approvals (Start the agent here) |
| **Grafana** | http://localhost:3000 | Explore → pick Prometheus / Loki / Tempo |
| **Prometheus** | http://localhost:9090/graph | paste PromQL, hit Execute, Graph tab |

In the **Agent UI click Start** — it now launches with `configs/demo.json`, which enables
metrics + traces, so the agent runs **log + metric + trace detection** and verifies recovery
on the metric it detected on.

### The four panels to keep on screen (Grafana Explore → Prometheus)
```promql
# 1. request rate per service
sum by (service) (rate(http_requests_total[1m]))
# 2. 5xx error ratio per service  (the agent's MetricErrorRatioDetector)
sum by (service) (rate(http_requests_total{status=~"5.."}[1m])) / sum by (service) (rate(http_requests_total[1m]))
# 3. real p95 latency in ms      (the agent's MetricLatencyDetector)
histogram_quantile(0.95, sum by (service,le) (rate(http_request_duration_seconds_bucket[1m]))) * 1000
# 4. worker job-queue backlog    (the agent's SaturationDetector)
queue_depth
```
Traces (Grafana Explore → **Tempo** → Search): Service Name `api`, Run → click a trace → the
waterfall across `webapp → api → auth`. TraceQL for just the bad ones: `{ status = error }`.

---

## The fix-vs-escalate rule (say this once)

The agent **diagnoses with the LLM, then acts within hard-coded safety tiers**:
- **Auto-fix (Tier 1):** restart a **stateless** service — `api, webapp, auth, payments, worker, gateway, loadgen`.
- **Approval (Tier 2):** anything touching a **stateful** service (`redis`, `postgres`) or risky ops (queue flush, config change) → waits for a human in the UI.
- **Escalate to human (Tier 3):** guardrail tripped (e.g. restart cap), unknown/invalid action, or it tried and recovery kept failing.
- **Never:** destructive operations on Postgres data — in any tier.

Metrics/traces change *how fast and how well it detects and verifies* — never the tier.

---

## Fault 1 — API error spike  →  **AUTO-FIX**

```powershell
docker exec api python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/on',method='POST'))"
```
**Show:** panel #2 (5xx ratio for `api`/`webapp` climbs toward ~0.7); Tempo `{ status = error }`
turns up red api spans. **Agent:** raises `api:metric_error_ratio` (+ log `error_rate`), diagnoses,
**restarts api (Tier 1 auto)**, then verifies the 5xx ratio dropped back under 20% before resolving.
**Heal:**
```powershell
docker exec api python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/off',method='POST'))"
```

## Fault 2 — API latency  →  **AUTO-FIX** (the metric headline)

```powershell
docker exec api python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/latency/on',method='POST'))"
```
**Show:** panel #3 — `api` p95 jumps from ~50 ms to 2000–6000 ms (a **real histogram**, not a log
guess); in Tempo the api span is visibly long. **Agent:** raises `api:metric_latency_p95`, restarts
api (Tier 1), and — the robustness fix — only marks **recovered when the p95 is back under
threshold**, not merely when errors clear. **Heal:** same command with `latency/off`.

## Fault 3 — Worker killed (the silent one)  →  **AUTO-FIX**

```powershell
docker stop worker
```
**Show:** panel #1 worker request line flatlines; panel #4 `queue_depth` climbs as jobs pile up with
no consumer. **Agent:** raises `worker:silence` (and `metric_saturation` as the backlog grows),
**restarts worker (Tier 1)**, verifies it logs again and the queue drains. **Heal:** `docker start worker`
(the agent will also do this itself when acting live).

## Fault 4 — Auth tier failing (cascade)  →  **AUTO-FIX at the root**

```powershell
docker exec auth python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/on',method='POST'))"
```
**Show:** panel #2 — 5xx rises on `auth` **and** its dependents `api`/`webapp` at once; Tempo shows the
error originating in the auth span. **Agent:** correlation collapses the fan-out into **one**
`auth:unreachable` incident rooted at auth, then **restarts auth (Tier 1)** — it fixes the root, not the
symptom. **Heal:** same with `errors/off` on `auth`.

## Fault 5 — Payments provider failing  →  **AUTO-FIX at the root**

```powershell
docker exec payments python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/on',method='POST'))"
```
**Show:** `worker` errors trying to charge; `queue_depth` grows (jobs requeue). **Agent:** one
`payments:unreachable` incident rooted at payments → **restart payments (Tier 1)**. **Heal:** `errors/off`
on `payments`.

## Fault 6 — Postgres down  →  **ESCALATE → HUMAN APPROVAL (Tier 2)**

```powershell
docker stop postgres
```
**Show:** panel #2 — `db_unreachable` 5xx cascade across api/webapp/worker. **Agent:** correlation roots
it at **postgres**, which is **stateful** → it does **not** auto-restart. The incident goes to
**AWAITING_APPROVAL**; the UI shows a proposal with blast radius. **You approve in the UI** → it restarts
postgres → verifies. This is the "approval only for certain things" path. **Heal (if you don't approve):**
`docker start postgres`.

## Fault 7 — Redis down  →  **ESCALATE → HUMAN APPROVAL (Tier 2)**

```powershell
docker stop redis
```
**Show:** `cache_degraded` warnings, then `redis_unreachable`. **Agent:** roots at **redis** (stateful) →
**approval required**, same as Postgres. **Heal:** `docker start redis`.

## Fault 8 — Memory leak / OOM  →  **AUTO-FIX**

```powershell
docker exec api python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/memleak/start',method='POST'))"
```
**Show:** memory climbing to the 256 MB cap → OOM kill → restart. Reliable views:
```powershell
docker stats api --no-stream    # MEM USAGE / LIMIT marches toward 256 MiB, then the container restarts
```
and in Grafana Explore → **Loki**: `{service="api"} |= "memory_pressure"` (the growing-allocation warnings),
then a fresh `startup` line after the OOM restart. **Agent:** the repeated `startup` events trip
`api:crash_loop`, and it **restarts api (Tier 1)**. **Heal:** `docker restart api` if it hasn't settled.
*(Note: cAdvisor is included but on Docker Desktop it often can't label metrics by container name, so use
`docker stats` / Loki for the memory view rather than a per-service PromQL.)*

## Show the escalation path on purpose

Trigger Fault 1 (errors) and let the agent restart api **3 times within an hour**. After the
**restart cap (2/hr in demo config)** is hit, the next attempt is **blocked by the guardrail and the
incident ESCALATES to a human** — exactly the lifecycle you saw. This demonstrates the agent knowing
when to stop trying and hand off.

---

## Reset between scenarios
```powershell
docker start postgres redis worker 2>$null
docker exec api      python -c "import urllib.request;[urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/%s/off'%s,method='POST')) for s in ['errors','latency']]"
docker exec auth     python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/off',method='POST'))"
docker exec payments python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/off',method='POST'))"
docker compose restart gateway   # clears any stale upstream after restarts
```

## The one-sentence pitch while it runs
"Same agent, three kinds of eyesight — it catches the fault on a real metric, shows you exactly where
in the request it broke via the trace, fixes what's safe to fix itself, and asks a human before
touching anything stateful."
