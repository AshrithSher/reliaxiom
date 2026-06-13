# RUNNING.md — try the SRE agent yourself

The agent tails the lab's log stream, detects faults with deterministic code, diagnoses
confirmed incidents with an LLM, and then **either fixes them automatically, asks a human to
approve a riskier fix, or hands off to a human** — depending on the action's risk tier.

This guide drives every one of those outcomes as a numbered scenario.

---

## 1. One-time setup

```powershell
cd C:\Mine\Concave\SRE
python -m venv .venv
.\.venv\Scripts\Activate.ps1           # activate ONCE (see note below)
python -m pip install -e ".[dev,dashboard]"   # dashboard extra = fastapi + uvicorn
```

Confirm the LLM key exists (gitignored): `.secrets\llm.env` with `GEMINI_API_KEY=...`.

> **Activate once per terminal.** After `Activate.ps1`, just type `python ...` — no venv path.
> In **cmd.exe** use `.venv\Scripts\activate.bat` instead. You'll use **two terminals**:
> **A** = the agent, **B** = control (faults / approvals / inspection). Activate the venv in both.

---

## 1b. Run the live demo — the dashboard is the whole console

The **only** thing in a terminal is the lab. Everything else — starting the agent, injecting
faults, approving Tier-2 actions — happens in the dashboard UI.

```powershell
.\scripts\demo-up.ps1            # lab (docker compose) + dashboard, opens http://localhost:8000
.\scripts\demo-up.ps1 -Reset     # same, but clear prior incident state first
```

Then, in the browser at `http://localhost:8000`:

1. **Start agent** — top-left button. Runs the SRE agent (`--execute --jira --confluence`) as a
   managed subprocess; its live log is behind the **Agent log** button.
2. **Inject a fault** — click one (e.g. **Auth tier failing**). Amber buttons (Postgres/Redis)
   are Tier-2 (need approval).
3. **Watch** the **service topology** cascade red and collapse to a single incident, the
   **incident feed** fill in, and the KPI strip (auto-resolve %, MTTR, pages avoided) update.
   Click an incident for its lifecycle timeline + the LLM root cause; **Jira ↗** links open the
   real ticket.
4. **Approve / reject** Tier-2 proposals inline in the incident drawer.
5. **Reset lab** clears all chaos and restarts any stopped containers.

Tickets land in **Jira** (project HELP) and post-mortems in **Confluence** (space ReliAxiom) by
default — the local SQLite/markdown mirror keeps working if Atlassian is unreachable. Tear down
with `.\scripts\demo-down.ps1` (`-Reset` to wipe state, `-Hard` to remove the lab).

> The dashboard's read side is a pure projection over the agent's SQLite stores; the control
> side (agent/faults/approvals) is orchestration only — neither changes detection/diagnosis
> logic. Reliability: with both `OPENROUTER_API_KEY` and `GEMINI_API_KEY` in `.secrets\llm.env`,
> diagnosis chains them (FallbackProvider) so a free-tier 429 can't kill a live demo.

The §5 terminal scenarios below still work for manual/automated runs, but the dashboard is the
intended demo surface.

---

## 2. Start the lab (the system being watched)

```powershell
docker compose -f C:\Mine\Concave\logs-streaming-demo-app\docker-compose.yml up -d
docker compose -f C:\Mine\Concave\logs-streaming-demo-app\docker-compose.yml ps   # 7 "Up"
```

---

## 3. Run the offline tests (no Docker / no API needed)

```powershell
python -m pytest tests -q                # expect 193 passed
```

---

## 4. Reset helper (run before each scenario)

The agent persists state in `.state\`. **Always stop the agent and clear state between
scenarios**, or a new fault dedups onto the old incident.

```powershell
# Terminal A: Ctrl+C to stop the agent first, then:
Remove-Item .state -Recurse -Force -ErrorAction SilentlyContinue
docker compose -f C:\Mine\Concave\logs-streaming-demo-app\docker-compose.yml start postgres redis worker
docker exec api python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/off', method='POST'))"
```

Start the agent (Terminal A). `--execute` lets it actually act; drop it for dry-run:

```powershell
python -m sre_agent.main --config configs\fast-demo.json --execute
```

You'll see a **status heartbeat every 15s** so you always know what it's doing:

```
# [09:43:02] healthy · 78 lines · 4 services · containers: all up · queue=0
# [09:44:20] DEGRADED · 976 lines · 4 services · containers: DOWN=worker · queue=33
#    INC-1 [SRE-1] worker:silence → VERIFYING — watching for recovery (28s left, else retry/escalate)
```

---

## 5. The scenarios

Every fault below is injected from **Terminal B**. Watch **Terminal A** (heartbeat + events).
Reset (§4) between each.

| # | Outcome | Trigger | Who fixes it |
|---|---------|---------|--------------|
| A2 | **Cascade → one incident** (auth tier) | auth chaos errors (see below) | agent auto-restarts auth |
| A3 | **Provider outage** (payments) | payments chaos errors | agent auto-restarts payments |
| A | **Auto-resolve** (Tier-1) | `docker stop worker` | agent, no human |
| B | **Approve → agent fixes** (Tier-2) | `docker stop postgres` | human approves, agent acts |
| C | **Reject → human fixes** (Tier-2) | `docker stop postgres` | human rejects + fixes |
| D | **Approval timeout → human** | `docker stop postgres` | nobody responds → escalate |
| E | **Can't fix → escalate** (guardrail) | kill worker repeatedly | agent gives up → human |
| F | **Act → no recovery → retry → escalate** | kill worker during verify | agent retries, then human |
| G | **One fault = one ticket** (correlation) | `docker stop postgres` | (observation) |
| H | **Maintenance silence** | any fault, agent in `--maintenance` | (observation) |
| I | **Healthy / null** | nothing | (observation) |

### A — Auto-resolve (Tier-1, no human)
Restarting a *stateless* service (worker/api/webapp/gateway) is auto-tier.

```powershell
# Terminal B
docker stop worker
```
**Agent:** detects silence → diagnoses "worker hung" → runs `docker restart worker` →
verifies it logs again → **auto-resolves** the ticket. Tags its own restart so it doesn't
alarm on the recovery.
**Verify:**
```powershell
docker inspect worker --format '{{.State.Status}}'    # running
python -c "import sqlite3; c=sqlite3.connect(r'.state\incidents.db'); print([(r[0],r[1]) for r in c.execute('select id,state from incidents')])"
# → [('INC-1','RESOLVED')]
```

### A2 — Auth tier cascade → one incident (the marquee demo)
The api authenticates every request via the **auth** service. Make auth start failing and the
whole front tier (api → webapp → gateway → loadgen) errors at once — but the agent collapses the
fan-out into a **single** `auth:unreachable` incident rooted at auth, then auto-restarts it.

```powershell
# Terminal B — make the auth tier fail (it stays up, fails fast, no DNS hang):
docker exec auth python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/on', method='POST'))"
```
**Agent:** one `auth:unreachable` ticket (not five) → diagnoses → `restart_container auth`
(stateless ⇒ Tier-1) → restart resets the in-memory fault → verifies errors cleared →
**auto-resolves**. Watch the topology map on the dashboard cascade red and collapse to one node.
Heal manually if needed: `docker exec auth python -c "...chaos/errors/off..."` or `docker restart auth`.

### A3 — Payments provider outage
The worker charges every order via the **payments** service. Make it fail:

```powershell
docker exec payments python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://localhost:5000/chaos/errors/on', method='POST'))"
```
**Agent:** `payments:unreachable` incident (worker can't charge orders) → auto-restarts payments
→ resolves.

### B — Approve → agent fixes (Tier-2)
Restarting a *stateful* service (postgres/redis) is approval-tier — the agent proposes and
waits.

```powershell
# Terminal B
docker stop postgres                  # clean db_unreachable cascade → one postgres incident
```
**Agent:** one `postgres:unreachable` ticket → diagnoses → proposes `restart_container
postgres` → **AWAITING_APPROVAL** (the heartbeat shows the exact approve command). Then:
```powershell
python -m sre_agent.approve list
python -m sre_agent.approve approve INC-1 --by you@example.com --execute
```
**Agent:** runs `docker restart postgres` → verifies → **resolves**; the approver is recorded
on the ticket.

> The LLM picks the action *kind* (restart_container); the **target is pinned in code to the
> correlated root_service** (postgres here), so a db outage always proposes a postgres restart
> (Tier-2) and reaches AWAITING_APPROVAL — the model can't downgrade it by naming a symptom
> service. See DECISIONS.md D-029.

### C — Reject → human fixes (Tier-2)
```powershell
# Terminal B — after the approval prompt appears:
python -m sre_agent.approve reject INC-1 --by you@example.com
docker start postgres                 # you fix it manually
```
**Agent:** **ESCALATED**, no action taken — the ticket is assigned to on-call.

### D — Approval timeout → human
Same as B, but **don't respond.** After `approval_timeout_s` (180s in fast-demo) the agent
**escalates with no action**. Heal manually: `docker start postgres`.

### E — Can't fix → escalate (guardrail)
`max_restarts_per_hour` is **2** in fast-demo. Kill the worker a third time:

```powershell
# Terminal B — repeat 3 times, waiting for the agent to restart+resolve between each:
docker stop worker      # restart #1 (auto-resolves)
docker stop worker      # restart #2 (auto-resolves)
docker stop worker      # restart #3 → cap reached → ESCALATED, no action
```
**Agent:** on the 3rd, the guardrail blocks the restart → **ESCALATED** ("restart cap
reached"). Heal: `docker start worker`.

### F — Act → no recovery → retry → escalate
Kill the worker and **keep killing it** each time the agent restarts it (during the
verification window). Recovery never holds → the agent re-diagnoses (bounded by
`max_remediation_loops`) → then **escalates**.

### G — One fault = one ticket (correlation)
```powershell
docker stop postgres
```
The api/webapp/worker/loadgen all error, but the agent opens **one** `postgres:unreachable`
ticket, not one per service. Inspect:
```powershell
python -c "import sqlite3,sys; sys.stdout.reconfigure(encoding='utf-8'); c=sqlite3.connect(r'.state\tickets.db'); c.row_factory=sqlite3.Row; [print(r['id'],r['fingerprint'],r['severity'],r['status']) for r in c.execute('select * from tickets')]"
```

### H — Maintenance silence
Start the agent with `--maintenance`, then inject any fault → **zero tickets, zero
notifications** (intended for running fault drills without alerting).
```powershell
python -m sre_agent.main --config configs\fast-demo.json --maintenance
```

### I — Healthy / null
Do nothing. The agent prints `healthy` heartbeats and opens no incidents — silence on a
healthy system is the headline behavior.

---

## 6. Inspect what happened (any time)

```powershell
# tickets + their lifecycle
python -c "import sqlite3,sys; sys.stdout.reconfigure(encoding='utf-8'); c=sqlite3.connect(r'.state\tickets.db'); c.row_factory=sqlite3.Row; [print(r['id'],r['fingerprint'],r['severity'],r['status']) for r in c.execute('select * from tickets')]"

# every comment (diagnosis, action, approval, resolution)
python -c "import sqlite3,sys; sys.stdout.reconfigure(encoding='utf-8'); c=sqlite3.connect(r'.state\tickets.db'); c.row_factory=sqlite3.Row; [print('-',r['author']+':',r['body']) for r in c.execute('select author,body from comments')]"

# agent's own actions (change log)
python -c "import sqlite3,sys; sys.stdout.reconfigure(encoding='utf-8'); c=sqlite3.connect(r'.state\changes.db'); c.row_factory=sqlite3.Row; [print('-',r['actor'],r['change_type'],r['service']) for r in c.execute('select actor,change_type,service from changes')]"
```

---

## 6b. Incident memory + post-mortems (M6)

Every resolved/escalated incident writes a **post-mortem** — structured (for memory) and a
markdown file under `.state\postmortems\`. The **second** time the same fault occurs, the
agent's diagnosis prompt includes the prior post-mortem ("we've seen this; X fixed it").

```powershell
# after running a scenario to resolution:
type .state\postmortems\INC-1.md            # the human-readable post-mortem
python -m sre_agent.report                  # weekly-style health report (volume, recurring faults, auto-resolve %)
```

To *see* memory in action: run scenario A (kill-worker) twice without clearing `.state` —
the second incident's diagnosis is informed by the first's post-mortem.

## 6c. Real Jira + Confluence (M7, opt-in)

By default tickets/post-mortems are local SQLite/markdown. To use real Atlassian, put
credentials in gitignored `.secrets\atlassian.env`:

```
ATLASSIAN_SITE=https://your-site.atlassian.net
ATLASSIAN_EMAIL=you@example.com
ATLASSIAN_API_TOKEN=...        # id.atlassian.com/manage-profile/security/api-tokens
JIRA_PROJECT_KEY=HELP
CONFLUENCE_SPACE_KEY=ReliAxiom
```

Then run with the flags (SQLite/markdown still mirror locally; Confluence publish is
best-effort so an outage never blocks the agent):

```powershell
python -m sre_agent.main --config configs\fast-demo.json --execute --jira --confluence
```

Incidents become Jira issues in your project (severity→priority, lifecycle→workflow,
fingerprint as a dedup label); resolved/escalated incidents publish a post-mortem page to
your Confluence space.

## 7. Automated scoring (no manual agent)

Stop the manual agent first (it runs its own).

```powershell
python -m eval.harness --scenario kill-worker     # detection scoring
python -m eval.harness --scenario auth-down        # auth-tier cascade → one incident
python -m eval.harness --scenario payments-down    # payments provider outage
python -m eval.harness --all                        # every scenario in sequence
python -m eval.harness --null --duration 900      # healthy → must stay silent
python -m eval.diagnosis_eval                      # diagnosis accuracy vs ground truth (~4/5)
```

---

## 8. Where to read the code

| Layer | Path | Start here |
|-------|------|-----------|
| Data objects | `sre_agent/models.py` | `IncidentCandidate`, `LogRecord` |
| Ingestion | `sre_agent/ingest/` | `tailer.py` → `parser.py` → `window.py` |
| Detection | `sre_agent/detect/` | `engine.py` (debounce/cooldown), then each detector |
| Pollers | `sre_agent/poll/` | `pollers.py`, `store.py`, `adapters.py` |
| Incidents | `sre_agent/incident/` | `manager.py`, then `correlation.py`, `topology.py`, `lifecycle.py` |
| Diagnosis | `sre_agent/diagnosis/` | `diagnoser.py`, `context.py`, `llm.py` |
| Actions | `sre_agent/action/` | `catalog.py` (tiers), `executor.py` (guardrails), `recovery.py` |
| Integrations | `sre_agent/integrations/` | `ticketing.py`, `notifications.py` (stubs; JIRA/Teams at M7) |
| Wiring | `sre_agent/main.py` + `approve.py` | `build_engine()`, `manager.step()`, approval CLI |

Design rationale for any choice is in `DECISIONS.md` (D-001…D-020).

---

## Gotchas (these bite)
1. **Stop the agent before clearing `.state`** — it holds the SQLite files (else "file in use").
2. **Reset between scenarios** (§4) — or a fault dedups onto the previous incident.
3. **One agent at a time** — don't run a manual agent and the eval harness together.
4. **`--execute` actually restarts containers.** Omit it to dry-run (decisions only).
5. **Gemini quota is per Google *project*** — a new key in the same project shares the same
   limit. `429`/`undetermined` escalations mean the key is rate-limited.
