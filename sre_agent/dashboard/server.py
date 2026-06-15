"""Read-only demo dashboard backend (FastAPI).

A live projection over the agent's persisted SQLite stores — incidents, tickets (the comment
trail is a ready-made timeline), the change log (audit trail), and post-mortems (ROI metrics).
It opens those stores **read-only and out-of-process**; it never imports or drives the
detection/diagnosis/action core. Live container/queue gauges come from the dashboard's *own*
light poll, because the agent keeps its SignalStore in memory. See DECISIONS.md D-025.

Run:
    uvicorn sre_agent.dashboard.server:app --port 8000
Config is taken from $SRE_DASHBOARD_CONFIG (a Config JSON) if set, else defaults.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from sre_agent.changelog import ChangeLog
from sre_agent.config import Config
from sre_agent.dashboard import control
from sre_agent.dashboard.metrics import compute_metrics
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.postmortems import SqlitePostMortemStore
from sre_agent.integrations.ticketing import SqliteTicketStore
from sre_agent.main import build_postmortem_store, build_ticket_store, load_secrets
from sre_agent.poll.store import PollCycle, PollLoop, SignalStore

_STATIC = Path(__file__).parent / "static"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STREAM_INTERVAL_S = 1.5

load_secrets()   # make Atlassian creds available for Jira links + approvals
_runner = control.AgentRunner(_REPO_ROOT, os.environ.get("SRE_DASHBOARD_CONFIG") or "configs/demo.json")

# states the agent considers "still in flight" — drives the topology heat
_ACTIVE_STATES = {
    IncidentState.DETECTED, IncidentState.DIAGNOSING, IncidentState.AWAITING_APPROVAL,
    IncidentState.ACTING, IncidentState.VERIFYING, IncidentState.FLAPPING,
}


def _cfg() -> Config:
    return Config.load(os.environ.get("SRE_DASHBOARD_CONFIG") or None)


def _jira_site() -> str | None:
    site = os.environ.get("ATLASSIAN_SITE")
    return site.rstrip("/") if site else None


def _jira_url(ticket_id: str | None) -> str | None:
    """A browse URL when the ticket id is a real Jira key (project-key prefix + site set)."""
    site = _jira_site()
    project = os.environ.get("JIRA_PROJECT_KEY")
    if site and project and ticket_id and ticket_id.startswith(project + "-"):
        return f"{site}/browse/{ticket_id}"
    return None


def _integration_links() -> dict:
    site = _jira_site()
    project = os.environ.get("JIRA_PROJECT_KEY")
    space = os.environ.get("CONFLUENCE_SPACE_KEY")
    cfg = _cfg()
    return {
        "jira": f"{site}/jira/software/projects/{project}/boards" if site and project else None,
        "confluence": f"{site}/wiki/spaces/{space}" if site and space else None,
        "ticketing_backend": cfg.ticketing_backend,
        "postmortems_backend": cfg.postmortems_backend,
    }


app = FastAPI(title="SRE Agent — Live Dashboard")
_signals = SignalStore()      # filled by the dashboard's own poll loop (best-effort)
_poll: PollLoop | None = None


# --- store access (fresh connections per read: safe across processes, cheap at demo scale) ---
def _data_dir() -> Path:
    return Path(_cfg().data_dir)


def _incident_store() -> IncidentStore:
    return IncidentStore(_data_dir() / "incidents.db")


def _all_incidents(store: IncidentStore, limit: int = 60) -> list[Incident]:
    """Active incidents plus recently-closed ones (resolved/escalated), newest first."""
    closed = (store.find_by_state(IncidentState.RESOLVED)
              + store.find_by_state(IncidentState.ESCALATED))
    incidents = store.find_active() + closed
    incidents.sort(key=lambda i: i.updated_at, reverse=True)
    return incidents[:limit]


def _incident_summary(inc: Incident) -> dict:
    return {
        "id": inc.id, "fingerprint": inc.fingerprint, "ticket_id": inc.ticket_id,
        "state": inc.state.value, "severity": inc.severity, "root_service": inc.root_service,
        "services": inc.services, "fault_type": inc.fault_type, "root_cause": inc.root_cause,
        "action_taken": inc.action_taken, "pending_action": inc.pending_action,
        "pending_params": inc.pending_params,
        "first_seen": inc.first_seen.isoformat(), "updated_at": inc.updated_at.isoformat(),
        "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
        "approval_deadline": (inc.approval_deadline.isoformat()
                              if inc.approval_deadline else None),
        "active": inc.state in _ACTIVE_STATES,
        "awaiting_approval": inc.state is IncidentState.AWAITING_APPROVAL,
        # escalated/flapping incidents are human-owned: the agent won't auto-close them, so the
        # operator who fixed it out-of-band needs an explicit Resolve action.
        "human_resolvable": inc.state in (IncidentState.ESCALATED, IncidentState.FLAPPING),
        "jira_url": _jira_url(inc.ticket_id),
    }


def _topology(active: list[Incident], down: set[str] | None = None) -> dict:
    """Nodes/edges from the lab topology, each node tagged healthy / affected / root.

    A node is shown red ('root') if it is the root of an active incident OR its container is
    currently down per the live poll — so a stopped service surfaces immediately, before
    detection has debounced an incident, and clears the instant the operator restarts it (the
    poll sees it 'running' again). Without this the map is driven purely by incidents and a
    down container with no incident yet renders deceptively green."""
    down = down or set()
    edges = []
    nodes: set[str] = set()
    for svc, deps in LAB_TOPOLOGY._direct.items():   # noqa: SLF001 — read-only projection
        nodes.add(svc)
        for dep in deps:
            nodes.add(dep)
            edges.append({"source": svc, "target": dep})
    roots = {i.root_service for i in active} | (down & nodes)
    affected: set[str] = set()
    for i in active:
        affected.update(i.services)
    return {
        "nodes": [{"id": n,
                   "health": "root" if n in roots else ("affected" if n in affected
                                                         else "healthy")}
                  for n in sorted(nodes)],
        "edges": edges,
    }


def _down_containers() -> set[str]:
    """Lab services whose container is not 'running' per the dashboard's own poll."""
    snap = _signals.latest()
    if snap is None:
        return set()
    return {name for name, cs in snap.containers.items() if cs.status != "running"}


def _signals_view() -> dict:
    snap = _signals.latest()
    if snap is None:
        return {"available": False}
    return {
        "available": True,
        "ts": snap.ts.isoformat(),
        "containers": {name: cs.status for name, cs in snap.containers.items()},
        "health": snap.health,
        "redis_queue_depth": snap.redis_queue_depth,
        "pg_connections": snap.pg_connections,
    }


def _build_state() -> dict:
    store = _incident_store()
    incidents = _all_incidents(store)
    active = [i for i in incidents if i.state in _ACTIVE_STATES]
    postmortems = SqlitePostMortemStore(_data_dir() / "postmortems.db").all()
    metrics = compute_metrics(postmortems, store.find_active())
    topology = _topology(active, _down_containers())
    # a stopped lab container is 'degraded' even before an incident exists — the live poll is
    # ground truth, so the header dot can't read green while postgres is down.
    down_in_topology = any(n["health"] == "root" for n in topology["nodes"])
    degraded = bool(active) or down_in_topology
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "health": "degraded" if degraded else "healthy",
        "incidents": [_incident_summary(i) for i in incidents],
        "topology": topology,
        "metrics": metrics,
        "signals": _signals_view(),
        "agent": {**_runner.status(), "log_tail": _runner.logs()[-40:]},
        "integrations": _integration_links(),
    }


# --- routes -------------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/api/state")
def state() -> JSONResponse:
    return JSONResponse(_build_state())


@app.get("/api/incident/{incident_id}")
def incident_detail(incident_id: str) -> JSONResponse:
    store = _incident_store()
    inc = store.get(incident_id)
    if inc is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    timeline = _timeline(inc)
    return JSONResponse({**_incident_summary(inc), "timeline": timeline})


def _timeline(inc: Incident) -> list[dict]:
    """Merge the ticket comment trail (diagnosis/action/approval/resolution) with the change
    log (the agent's own actions) into one timestamp-ordered lifecycle."""
    events: list[dict] = [{"ts": inc.first_seen.isoformat(), "kind": "detected",
                           "text": f"Detected {inc.fingerprint} ({inc.severity})"}]
    if inc.ticket_id:
        ticket = SqliteTicketStore(_data_dir() / "tickets.db").get(inc.ticket_id)
        if ticket:
            for c in ticket.comments:
                events.append({"ts": c.ts.isoformat(), "kind": "comment",
                               "text": f"[{c.author}] {c.body}"})
    changelog = ChangeLog(_data_dir() / "changes.db")
    window_start = inc.first_seen - timedelta(seconds=10)
    window_end = (inc.resolved_at or inc.updated_at) + timedelta(seconds=10)
    for e in changelog.recent(window_start, window_end):
        if e.service in inc.services or e.actor == "sre-agent":
            events.append({"ts": e.ts.isoformat(), "kind": "action",
                           "text": f"{e.actor} {e.change_type} {e.service} {e.detail}".strip()})
    events.sort(key=lambda x: x["ts"])
    return events


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    async def gen():
        while True:
            payload = await asyncio.to_thread(_build_state)
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(_STREAM_INTERVAL_S)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# --- control plane: agent lifecycle, fault injection, approvals -------------------------
@app.get("/api/faults")
def faults() -> JSONResponse:
    return JSONResponse([
        {"name": name, "label": f["label"], "tier": f["tier"], "blurb": f["blurb"]}
        for name, f in control.DEMO_FAULTS.items()])


@app.post("/api/control/inject/{name}")
def inject(name: str) -> JSONResponse:
    return JSONResponse(control.run_fault(name, _cfg().compose_dir))


@app.post("/api/control/reset")
def reset() -> JSONResponse:
    return JSONResponse(control.reset_lab(_cfg().compose_dir))


@app.post("/api/agent/start")
def agent_start() -> JSONResponse:
    return JSONResponse(_runner.start())


@app.post("/api/agent/stop")
def agent_stop() -> JSONResponse:
    return JSONResponse(_runner.stop())


@app.get("/api/agent/logs")
def agent_logs() -> JSONResponse:
    return JSONResponse({"running": _runner.running, "logs": _runner.logs()})


def _stores_for_approval(cfg: Config):
    data_dir = Path(cfg.data_dir)
    return build_ticket_store(cfg, data_dir), build_postmortem_store(cfg, data_dir)


@app.post("/api/incident/{incident_id}/approve")
def approve(incident_id: str, by: str = "operator") -> JSONResponse:
    cfg = _cfg()
    ticket_store, postmortems = _stores_for_approval(cfg)
    return JSONResponse(
        control.approve_incident(cfg, ticket_store, postmortems, incident_id, by))


@app.post("/api/incident/{incident_id}/reject")
def reject(incident_id: str, by: str = "operator") -> JSONResponse:
    cfg = _cfg()
    ticket_store, postmortems = _stores_for_approval(cfg)
    return JSONResponse(
        control.reject_incident(cfg, ticket_store, postmortems, incident_id, by))


@app.post("/api/incident/{incident_id}/resolve")
def resolve_incident(incident_id: str, by: str = "operator") -> JSONResponse:
    cfg = _cfg()
    ticket_store, postmortems = _stores_for_approval(cfg)
    return JSONResponse(
        control.resolve_incident(cfg, ticket_store, postmortems, incident_id, by))


# --- live gauges: the dashboard's own best-effort poll (never touches the agent) ----------
@app.on_event("startup")
def _start_polling() -> None:
    global _poll
    try:
        from sre_agent.poll.adapters import build_live_pollers
        cycle = PollCycle(build_live_pollers(_cfg()), _signals)
        _poll = PollLoop(cycle, interval_s=max(2.0, _cfg().poll_interval_s))
        _poll.start()
    except Exception:   # noqa: BLE001 — gauges are optional; the dashboard works without them
        _poll = None


@app.on_event("shutdown")
def _stop_polling() -> None:
    if _poll is not None:
        _poll.stop()


# static assets (js/css) under /static
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
