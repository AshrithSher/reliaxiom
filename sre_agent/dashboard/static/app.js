// SRE Agent live console — consumes /api/stream (SSE) for state, and drives the control plane
// (agent start/stop, fault injection, approvals) via POST endpoints. Single pane of glass.

const NODE_COLORS = { healthy: "#34d399", affected: "#fbbf24", root: "#f43f5e" };
const fmtSecs = (s) => (s == null ? "—" : s < 90 ? `${Math.round(s)}s` : `${(s / 60).toFixed(1)}m`);
const pct = (r) => `${Math.round((r || 0) * 100)}%`;
const $ = (id) => document.getElementById(id);

async function post(url) {
  try { const r = await fetch(url, { method: "POST" }); return await r.json(); }
  catch (e) { return { ok: false, error: String(e) }; }
}
function toast(msg, ok = true) {
  const t = $("toast");
  t.textContent = msg;
  t.className = `fixed bottom-4 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-lg text-sm shadow-lg ${ok ? "bg-emerald-700" : "bg-rose-700"} text-white`;
  t.style.display = "block";
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.style.display = "none"), 3200);
}

// ---- KPI strip ----
function renderKpis(m) {
  const cards = [
    { lbl: "Incidents", val: m.total }, { lbl: "Auto-resolved", val: pct(m.auto_resolve_rate) },
    { lbl: "Pages avoided", val: m.pages_avoided }, { lbl: "Mean resolve", val: fmtSecs(m.mttr_s) },
    { lbl: "Fastest fix", val: fmtSecs(m.fastest_resolve_s) }, { lbl: "Active now", val: m.active },
  ];
  $("kpis").innerHTML = cards.map((c) =>
    `<div class="kpi"><div class="val">${c.val}</div><div class="lbl">${c.lbl}</div></div>`).join("");
}

// ---- topology ----
let cy = null;
function initTopology(topo) {
  cy = cytoscape({
    container: $("topology"),
    elements: [
      ...topo.nodes.map((n) => ({ data: { id: n.id, health: n.health } })),
      ...topo.edges.map((e) => ({ data: { source: e.source, target: e.target } })),
    ],
    style: [
      { selector: "node", style: {
          "background-color": (n) => NODE_COLORS[n.data("health")] || NODE_COLORS.healthy,
          "label": "data(id)", "color": "#e2e8f0", "font-size": "11px",
          "text-valign": "center", "text-halign": "center", "text-margin-y": -18,
          "width": 30, "height": 30, "border-width": 2, "border-color": "#0f172a" } },
      { selector: "edge", style: {
          "width": 2, "line-color": "#334155", "target-arrow-color": "#334155",
          "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.8 } },
    ],
    layout: { name: "breadthfirst", directed: true, spacingFactor: 1.3, padding: 20 },
    userZoomingEnabled: false, userPanningEnabled: false, boxSelectionEnabled: false,
  });
}
function updateTopology(topo) {
  if (!cy) { initTopology(topo); return; }
  topo.nodes.forEach((n) => { const el = cy.getElementById(n.id); if (el) el.data("health", n.health); });
  cy.style().update();
}

// ---- incident feed ----
function incidentCard(i) {
  const pulse = i.active && !i.awaiting_approval ? "pulsing" : "";
  const sub = i.awaiting_approval ? `⏳ proposed: ${i.pending_action || "?"} — needs approval`
            : i.root_cause ? i.root_cause.slice(0, 90) : i.fault_type;
  const ref = i.jira_url ? `<a href="${i.jira_url}" target="_blank" class="text-sky-400 hover:underline" onclick="event.stopPropagation()">${i.ticket_id} ↗</a>`
            : `<span class="text-slate-400">${i.ticket_id || i.id}</span>`;
  return `<div class="inc-card ${pulse}" data-id="${i.id}">
      <div class="flex items-center justify-between mb-1">
        <span class="font-mono text-xs">${ref}</span>
        <span class="badge s-${i.state}">${i.state.replace("_", " ")}</span>
      </div>
      <div class="text-sm font-medium">${i.fingerprint}</div>
      <div class="text-xs text-slate-400 mt-0.5">${sub}</div>
    </div>`;
}
function renderIncidents(incidents) {
  const box = $("incidents");
  if (!incidents.length) { box.innerHTML = `<p class="text-slate-500 text-sm">No incidents — system healthy.</p>`; return; }
  box.innerHTML = incidents.map(incidentCard).join("");
  box.querySelectorAll(".inc-card").forEach((el) => el.addEventListener("click", () => openDrawer(el.dataset.id)));
}

// ---- charts ----
let mttrChart, outcomeChart, faultChart;
function renderCharts(m) {
  const baseOpts = { plugins: { legend: { labels: { color: "#94a3b8" } } },
                     scales: { x: { ticks: { color: "#64748b" } }, y: { ticks: { color: "#64748b" } } } };
  const labels = m.mttr_trend.map((t) => t.incident_id), data = m.mttr_trend.map((t) => t.ttr_s);
  if (!mttrChart) mttrChart = new Chart($("mttrChart"), { type: "line",
    data: { labels, datasets: [{ label: "TTR (s)", data, borderColor: "#38bdf8", backgroundColor: "rgba(56,189,248,0.15)", tension: 0.3, fill: true }] }, options: baseOpts });
  else { mttrChart.data.labels = labels; mttrChart.data.datasets[0].data = data; mttrChart.update(); }

  const od = [m.resolved, m.escalated, m.active];
  if (!outcomeChart) outcomeChart = new Chart($("outcomeChart"), { type: "doughnut",
    data: { labels: ["Auto-resolved", "Escalated", "Active"], datasets: [{ data: od, backgroundColor: ["#34d399", "#f43f5e", "#fbbf24"] }] },
    options: { plugins: { legend: { labels: { color: "#94a3b8" } } } } });
  else { outcomeChart.data.datasets[0].data = od; outcomeChart.update(); }

  const fl = Object.keys(m.by_fault_type), fd = Object.values(m.by_fault_type);
  if (!faultChart) faultChart = new Chart($("faultChart"), { type: "bar",
    data: { labels: fl, datasets: [{ label: "count", data: fd, backgroundColor: "#818cf8" }] }, options: baseOpts });
  else { faultChart.data.labels = fl; faultChart.data.datasets[0].data = fd; faultChart.update(); }
}

// ---- incident detail drawer (with approve/reject) ----
let lastState = null;
async function openDrawer(id) {
  const res = await fetch(`/api/incident/${id}`);
  if (!res.ok) return;
  const i = await res.json();
  const jira = i.jira_url ? ` · <a href="${i.jira_url}" target="_blank" class="text-sky-400 hover:underline">${i.ticket_id} ↗</a>` : "";
  $("drawer-title").innerHTML = `${i.fingerprint}${jira}`;
  const meta = [["State", i.state], ["Severity", i.severity], ["Root service", i.root_service],
    ["Affected", i.services.join(", ")], ["Action taken", i.action_taken || "—"]];
  const rows = meta.map(([k, v]) => `<div class="flex justify-between gap-4"><span class="text-slate-500">${k}</span><span class="text-right">${v}</span></div>`).join("");
  const rc = i.root_cause ? `<div class="bg-slate-800/60 rounded p-3"><div class="text-slate-500 text-xs mb-1">ROOT CAUSE (LLM)</div>${i.root_cause}</div>` : "";
  let approve = "";
  if (i.awaiting_approval) {
    approve = `<div class="bg-amber-950/40 border border-amber-800 rounded p-3">
      <div class="text-amber-300 text-xs mb-2">TIER-2 — proposes <b>${i.pending_action || "?"}</b> ${JSON.stringify(i.pending_params || {})}</div>
      <div class="flex gap-2">
        <button class="btn btn-primary" onclick="decide('${i.id}','approve')">Approve & run</button>
        <button class="btn btn-danger" onclick="decide('${i.id}','reject')">Reject → escalate</button>
      </div></div>`;
  } else if (i.human_resolvable) {
    // escalated/flapping = handed to a human; the agent won't auto-close it. If you fixed it
    // out-of-band (e.g. `docker start redis`), close the ticket here.
    approve = `<div class="bg-slate-800/60 border border-slate-700 rounded p-3">
      <div class="text-slate-300 text-xs mb-2">${i.state} — the agent handed this off. Fixed it yourself? Close it.</div>
      <button class="btn btn-primary" onclick="decide('${i.id}','resolve')">Mark resolved</button>
    </div>`;
  }
  const tl = (i.timeline || []).map((e) =>
    `<div class="tl-item k-${e.kind}"><div class="text-xs text-slate-500">${new Date(e.ts).toLocaleTimeString()}</div><div>${e.text}</div></div>`).join("");
  $("drawer-body").innerHTML = `<div class="space-y-1.5">${rows}</div>${approve}${rc}
     <div><div class="text-slate-400 font-semibold mb-2">Lifecycle</div>${tl || "<p class='text-slate-500'>No events yet.</p>"}</div>`;
  showDrawer("drawer");
}
async function decide(id, action) {
  const r = await post(`/api/incident/${id}/${action}`);
  toast(r.ok ? `${action}d ${id}${r.detail ? " — " + r.detail : ""}` : `${action} failed: ${r.error || r.detail}`, r.ok);
  closeDrawers();
}
window.decide = decide;

// ---- drawers / overlay ----
function showDrawer(which) {
  closeDrawers(true);
  $(which).classList.remove("translate-x-full");
  $("overlay").classList.remove("hidden");
}
function closeDrawers(keepOverlay) {
  $("drawer").classList.add("translate-x-full");
  $("logdrawer").classList.add("translate-x-full");
  if (!keepOverlay) $("overlay").classList.add("hidden");
}
$("drawer-close").addEventListener("click", () => closeDrawers());
$("logdrawer-close").addEventListener("click", () => closeDrawers());
$("overlay").addEventListener("click", () => closeDrawers());

// ---- control bar: agent lifecycle + fault injection ----
let faultsLoaded = false;
async function loadFaults() {
  const faults = await (await fetch("/api/faults")).json();
  $("fault-buttons").innerHTML = faults.map((f) =>
    `<button class="btn fault-btn tier-${f.tier}" title="${f.blurb}" onclick="injectFault('${f.name}','${f.label}')">${f.label}</button>`).join("");
  faultsLoaded = true;
}
async function injectFault(name, label) {
  const r = await post(`/api/control/inject/${name}`);
  toast(r.ok ? `Injected: ${label}` : `Inject failed: ${(r.errors || [r.error]).join("; ")}`, r.ok);
}
window.injectFault = injectFault;

$("btn-agent").addEventListener("click", async () => {
  const running = lastState?.agent?.running;
  const r = await post(running ? "/api/agent/stop" : "/api/agent/start");
  toast(running ? "Agent stopped" : "Agent started", true);
});
$("btn-reset").addEventListener("click", async () => {
  const r = await post("/api/control/reset");
  toast("Lab reset — chaos cleared, containers restarted", r.ok);
});
$("btn-logs").addEventListener("click", () => showDrawer("logdrawer"));

function renderAgent(agent) {
  const badge = $("agent-state"), btn = $("btn-agent");
  if (agent.running) {
    badge.textContent = `agent: running (pid ${agent.pid})`;
    badge.className = "badge bg-emerald-800 text-emerald-200";
    btn.textContent = "Stop agent"; btn.className = "btn btn-danger";
  } else {
    badge.textContent = "agent: stopped";
    badge.className = "badge bg-slate-700 text-slate-300";
    btn.textContent = "Start agent"; btn.className = "btn btn-primary";
  }
  const pre = $("agent-log");
  if (agent.log_tail) {
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.textContent = agent.log_tail.join("\n");
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }
}

function renderLinks(integrations) {
  const j = $("link-jira"), c = $("link-conf");
  if (integrations.jira) { j.href = integrations.jira; j.classList.remove("hidden"); }
  if (integrations.confluence) { c.href = integrations.confluence; c.classList.remove("hidden"); }
}

// ---- render a full state snapshot ----
function render(s) {
  lastState = s;
  $("health-dot").className = `w-3 h-3 rounded-full ${s.health === "healthy" ? "bg-emerald-400" : "bg-rose-500 pulsing"}`;
  renderKpis(s.metrics);
  updateTopology(s.topology);
  renderIncidents(s.incidents);
  renderCharts(s.metrics);
  if (s.agent) renderAgent(s.agent);
  if (s.integrations) renderLinks(s.integrations);
  if (!faultsLoaded) loadFaults();
}

// ---- live connection ----
function connect() {
  const conn = $("conn");
  const es = new EventSource("/api/stream");
  es.onmessage = (e) => { conn.textContent = "● live"; conn.className = "text-emerald-400"; render(JSON.parse(e.data)); };
  es.onerror = () => { conn.textContent = "○ reconnecting…"; conn.className = "text-amber-400"; };
}
fetch("/api/state").then((r) => r.json()).then(render).catch(() => {});
connect();
