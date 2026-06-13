# AGENT.md — behavioral specification of the SRE Agent

This is the contract for how the agent behaves at runtime. ARCHITECTURE.md describes the
components; this file describes the rules they must obey.

## Incident lifecycle (state machine)

```
DETECTED → DIAGNOSING → [AWAITING_APPROVAL] → ACTING → VERIFYING → RESOLVED
                ↑                                            |
                └────────── (max 2 loops) ───────────────────┘
                                  ↓
                              ESCALATED          FLAPPING ↔ (RESOLVED/DETECTED oscillation)
```

- **DETECTED** — a debounced Incident Candidate was confirmed and correlated. Ticket created
  (unless maintenance mode, or open ticket with same fingerprint → comment instead).
- **DIAGNOSING** — LLM diagnosis with assembled context. Output is structured (see below).
- **AWAITING_APPROVAL** — Tier 2 only. Proposal posted to the notification channel with
  symptom, diagnosis, evidence, proposed action, blast radius. Timeout 15 min → ESCALATED,
  no action taken.
- **ACTING** — execute exactly one catalog action. Action is recorded in the change log
  *before* execution (so detection can ignore the resulting noise).
- **VERIFYING** — evaluate the fingerprint's recovery predicate over a 3-minute window.
  Pass → RESOLVED. Fail → back to DIAGNOSING with the failed hypothesis attached.
  Maximum 2 diagnose→act→verify loops, then ESCALATED.
- **RESOLVED** — ticket auto-closed with closing comment; post-mortem generated.
- **ESCALATED** — ticket assigned to a human with everything found. The agent stops acting.
- **FLAPPING** — same fingerprint re-fires within the flap window (default 30 min) after a
  RESOLVED. The ticket is reopened once and held open; further oscillations are comments,
  not state churn. Flapping incidents escalate after 3 cycles.

## Correlation (before ticketing)

Candidates arriving within the correlation window (default 90 s) that share an upstream
dependency in the topology map are merged into one incident, attributed to the most
upstream suspect. Example: Redis OOM → api errors + worker silence + queue stall = **one**
incident with fingerprint `redis:oom`, not three.

## Diagnosis output (structured)

```json
{
  "root_cause": "...",
  "evidence": ["log line refs — verified to exist before acceptance"],
  "selected_action": "<catalog action id>",
  "action_params": { },
  "alternative_hypotheses": ["..."]
}
```

Note what is *absent*: the LLM does not output a tier and its confidence number is not
used for routing.

## Escalation triggers (calibration signals, not LLM confidence)

Escalate (Tier 3) when any of:
- Fingerprint not seen before (no history, no runbook match)
- Cited evidence refs do not resolve to real log lines (hallucination check)
- Two independent diagnosis runs disagree on root cause
- Selected action not in catalog, or params fail validation
- Any guardrail tripped (see below)
- Verification failed twice

## Action catalog and tiers

Tiers are **hard-coded per action in code**. The LLM only picks the action id.

| Action | Tier | Notes |
|--------|------|-------|
| `restart_container(service)` | 1 — auto | Stateless services only (webapp, api, worker, gateway) |
| `clear_stuck_queue_item(id)` | 1 — auto | Single item, logged |
| `rerun_failed_job(id)` | 1 — auto | Idempotent jobs only |
| `restart_container(redis)` | 2 — approval | Touches state |
| `flush_queue` | 2 — approval | Data loss possible |
| `change_config(service, key, value)` | 2 — approval | Whitelisted keys only |
| `restart_container(postgres)` | 2 — approval | Last resort; data never touched |
| anything else | 3 — escalate | Unknown = human |

## Guardrails (enforced in code, all tiers)

- Max 3 restarts per service per hour; breach → ESCALATED
- Never any action that deletes or mutates postgres data
- One action per ACTING pass — no chained actions without re-verification
- Dry-run mode: all actions log intent without executing (default for new scenarios)
- Maintenance mode suppresses ticket creation and notifications entirely
- Actions are idempotent against current reality: check container state / queue state
  before executing, so a crash-restart of the agent never double-executes

## Self-awareness

Every agent action is written to the change log with an `actor: sre-agent` tag before
execution. Detection consults the change log: anomalies within the expected blast radius
and time window of a tagged agent action do not create new candidates.

## Severity mapping

- Loadgen sees user-facing errors → **High**
- Background lag (worker, queue depth) → **Medium**
- Degraded but serving → **Low**

## Verification predicates

Recovery is a per-fingerprint predicate, not "looks normal". Each fingerprint defines
concrete conditions, e.g. `redis:oom` → loadgen p95 < threshold AND api error rate < baseline
AND queue depth monotonically decreasing over the window. Predicates live alongside the
fingerprint definitions and are evaluated by code, not the LLM.
