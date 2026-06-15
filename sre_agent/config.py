"""Agent configuration. Static defaults tuned to the lab (see DECISIONS.md D-010);
override per-run with a JSON file via --config."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    # Lab location (used by the tailer and the eval harness)
    compose_dir: str = r"C:\Mine\Concave\logs-streaming-demo-app"

    # Persistence
    data_dir: str = ".state"  # SQLite stores for tickets + incidents (restart-safe)

    # State & HA (Maturity 9 / ROADMAP P1.2). `state_backend` selects the persistence substrate
    # for ALL of the agent's control-plane stores — incidents, tickets, change log, post-mortems,
    # and the restart-cap ledger. "sqlite" (per-host files, the default) is restart-safe on one
    # host (D-009); "postgres" is a SHARED managed store so multiple replicas can run — making the
    # agent failure-safe, not just restart-safe. The DSN is read from the env var named by
    # `state_dsn_env` (never committed), falling back to `state_dsn`. This store is deliberately
    # SEPARATE from any monitored system's database (invariant #4: the agent never touches the
    # monitored system's data of record — this is the agent's own control plane).
    state_backend: str = "sqlite"          # "sqlite" | "postgres"
    state_dsn_env: str = "SRE_STATE_DSN"
    state_dsn: str = ""                    # e.g. "host=localhost port=5433 dbname=sre user=sre ..."
    # Leader election (HA): when on, replicas contend for one Postgres advisory lock; only the
    # leader drives the incident lifecycle (correlate → ticket → diagnose → act → verify). The
    # others stay warm standbys (tailing + detecting to keep their window hot) and take over on
    # leader loss. Incident state is in the shared store, so a standby resumes mid-incident
    # without re-executing actions (D-009 at fleet scale). Requires state_backend=postgres.
    ha_enabled: bool = False
    replica_id: str = ""                   # this replica's id; defaults to "<hostname>:<pid>"
    leader_lock_key: int = 911             # advisory-lock key the replicas contend for
    # Agent self-health HTTP server (k8s liveness/readiness probes → self-healing scheduling).
    # The Prometheus /metrics endpoint and SLO alerting are added in Maturity 11 on the same server.
    health_enabled: bool = False
    health_port: int = 9108

    # Integration backends (M7). Default to real Atlassian (Jira + Confluence); the composite
    # stores keep a local SQLite/markdown mirror and degrade to it if Atlassian is unreachable
    # or its credentials are missing, so this default is safe even offline.
    ticketing_backend: str = "jira"        # "jira" | "sqlite"
    postmortems_backend: str = "confluence"  # "confluence" | "local"
    jira_oncall_account_id: str = ""       # Atlassian accountId to assign escalations to

    # Sliding window
    window_max_age_s: int = 900  # keep 15 min of context for later layers

    # Telemetry sources (P0.1 / D-031). Detection reads through the TelemetrySource SPI; the
    # defaults keep the lab on the in-memory tailed-log path so the offline suite is untouched.
    # Flip a backend on to query real metrics/traces from the free Grafana LGTM stack.
    telemetry_logs: str = "tailed"        # "tailed" (SlidingWindow) | "loki"
    telemetry_metrics: str = "off"        # "off" | "prometheus"
    telemetry_traces: str = "off"         # "off" | "tempo"
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    tempo_url: str = "http://localhost:3200"
    # services that expose RED metrics / spans (the instrumented Flask tiers)
    metric_services: list[str] = field(
        default_factory=lambda: ["api", "webapp", "auth", "payments"]
    )
    metric_error_ratio_threshold: float = 0.2      # 5xx fraction per service → anomaly
    metric_latency_p95_threshold_ms: float = 1000.0  # real histogram p95, replaces log-derived

    # Topology source (P0.1 / D-031). The dependency graph is read through the TopologyProvider
    # SPI; "static" wraps the built-in LAB_TOPOLOGY literal (default), "http" fetches a
    # {service: [deps]} adjacency document from a service-mesh/CMDB/Backstage endpoint and
    # degrades to the static literal on any failure (D-015).
    topology_source: str = "static"   # "static" | "http"
    topology_url: str = "http://localhost:8080"

    # Diagnosis LLM (M3). Provider is swappable: "gemini" now, "anthropic" later — the
    # diagnosis layer only knows the LLMProvider interface. The API key is read from the
    # env var named here, never from this file.
    llm_provider: str = "openrouter"     # "openrouter" | "gemini" — the PRIMARY provider
    llm_fallback: bool = True            # if the other provider's key is also set, chain it
    #   behind the primary (FallbackProvider) so a free-tier 429 can't kill a live demo
    gemini_model: str = "gemini-flash-latest"
    gemini_api_key_env: str = "GEMINI_API_KEY"
    openrouter_model: str = "nvidia/nemotron-nano-9b-v2:free"  # free + reliably picks actions
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 30.0
    diagnosis_budget_chars: int = 6000   # rough token budget for the assembled prompt
    diagnosis_runs: int = 2              # independent runs; disagreement → escalate
    diagnosis_window_s: float = 600.0    # how far back to pull context logs

    # Action backend (P0.1 / D-037). The execution substrate is swappable behind the
    # ActionBackend SPI; "docker" is the lab default, "kubernetes" drives a local kind cluster
    # via the k8s API under a scoped ServiceAccount (deploy/k8s/rbac.yaml) — no cloud.
    action_backend: str = "docker"          # "docker" | "kubernetes"
    kube_namespace: str = "lab"
    kube_api_server: str = ""               # else read from $SRE_KUBE_API_SERVER
    kube_token_env: str = "SRE_KUBE_TOKEN"  # env var holding the ServiceAccount bearer token
    kube_ca_cert: str = ""                  # path to the API-server CA cert (optional)
    # Action rate-limit store (P0.5 / D-038). The restart cap is enforced atomically here, not
    # in Guardrails, so it holds across processes. "sqlite" (per-host, atomic) is the default;
    # "postgres" (a shared store for multi-host fleets) is the planned swap (P1.2).
    guardrail_store: str = "sqlite"         # "sqlite" | "postgres"

    # Action layer + verification (M4)
    dry_run: bool = True                 # safe by default: log intent, don't execute. --execute flips it
    max_restarts_per_hour: int = 3       # per service; breach → escalate
    action_suppression_s: float = 120.0  # ignore anomalies for a service this long after an agent action
    recovery_window_s: float = 180.0     # watch this long for recovery after acting
    max_remediation_loops: int = 2       # diagnose→act→verify loops before escalating
    max_diagnosis_attempts: int = 5      # retry a transiently-unavailable LLM this many times
    escalate_unknown_fingerprint: bool = False  # if on: a fault with no prior post-mortem
    #   escalates to a human instead of auto-acting (D-021). Off by default — it would block
    #   every first-occurrence auto-remediation; incident memory is the primary mechanism.
    recovery_check_s: float = 60.0       # look-back for "errors stopped" recovery checks
    recovery_error_tolerance: int = 2    # allow this many stray errors and still call it recovered

    # Incident Manager (M2)
    correlation_window_s: float = 90.0   # buffer candidates this long to collapse a cascade
    # Multi-signal correlation engine (P1.1 / D-040). The buffer above decides WHEN to
    # correlate; these tune the affinity graph itself. correlation_max_span_s is the adaptive
    # window for non-causal edges (same-service / topology / dep-error) — real cascades
    # propagate over minutes, not the 90s flush window; a SHARED TRACE id bypasses it entirely
    # (it is causal regardless of timing). change_coincidence_window_s is how far back a
    # deploy/config change counts as the likely root for a coincident cascade.
    correlation_max_span_s: float = 300.0
    change_coincidence_window_s: float = 600.0
    flap_window_s: float = 1800.0        # recurrence within this of resolution = flapping
    flap_escalate_after: int = 3         # this many reopens → hand to a human
    maintenance_mode: bool = False       # when on: no tickets, no notifications
    approval_timeout_s: float = 900.0    # Tier-2 approval wait before escalating (15 min)

    # Secondary-signal pollers
    poll_interval_s: float = 10.0
    poll_timeout_s: float = 5.0
    health_services: list[str] = field(default_factory=lambda: ["api", "webapp"])

    # Detection loop
    tick_interval_s: float = 5.0
    status_interval_s: float = 20.0  # how often to print a heartbeat/status line to stderr
    debounce_s: float = 120.0    # anomaly must persist this long to become a candidate
    grace_s: float = 20.0        # anomaly may blip out this long without resetting debounce
    cooldown_s: float = 600.0    # one candidate per (service, signal) per cooldown

    # error_rate detector
    error_rate_window_s: float = 60.0
    error_rate_threshold: int = 10  # ERROR lines per service per window

    # latency detector: p95 over the window, not average — a few slow requests must not
    # hide behind many fast ones, and one outlier must not page. Watches user-facing
    # services only (loadgen is the synthetic user; worker latency is background lag).
    latency_window_s: float = 60.0
    latency_p95_threshold_ms: float = 1000.0
    latency_min_samples: int = 10
    latency_services: list[str] = field(default_factory=lambda: ["loadgen"])

    # malformed-spike detector: corrupt-JSON lines per window (plain-text dependency logs
    # excluded — see parser). A flood means a service is emitting garbage or the stream
    # is corrupted.
    malformed_window_s: float = 60.0
    malformed_threshold: int = 10

    # crash-loop detector: repeated `startup` events mean a service is restarting in a
    # loop (e.g. memleak → OOM kill → docker restart → startup, over and over).
    crashloop_window_s: float = 300.0
    crashloop_threshold: int = 3  # startups within the window

    # queue-depth growth detector: worker heartbeats carry queue_depth. A backlog that is
    # both above threshold and still growing across the window is falling behind.
    queue_depth_window_s: float = 120.0
    queue_depth_threshold: int = 50
    queue_depth_min_samples: int = 2

    # silence detector: services expected to log steadily, and how long quiet = anomalous.
    # postgres/redis are excluded: they log rarely when healthy.
    silence_services: list[str] = field(
        default_factory=lambda: ["gateway", "webapp", "api", "worker", "loadgen"]
    )
    silence_threshold_s: float = 45.0  # worker heartbeats every ~15s; 3 missed = silent

    # container-down detector: a stopped/unhealthy stateful dependency is a fault in itself,
    # independent of how much its consumers happen to log. These are exactly the services the
    # silence detector excludes (they log rarely when healthy) and whose downstream error
    # cadence is too slow/short-circuited to reliably trip error_rate (a redis consumer retries
    # every few seconds; a db outage behind a broken auth tier never reaches the db at all).
    # Maps container name -> the dependency error code so a container-down candidate correlates
    # to the SAME `service:unreachable` fingerprint the log path produces (one fault = one
    # ticket). Driven by the polled SignalStore; runs through the engine, so it inherits
    # debounce/cooldown and action-suppression (won't alarm on the agent's own restarts).
    container_down_services: dict[str, str] = field(
        default_factory=lambda: {"postgres": "db_unreachable", "redis": "redis_unreachable"}
    )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg = cls()
        if path:
            overrides = json.loads(Path(path).read_text(encoding="utf-8"))
            valid = {f.name for f in fields(cls)}
            for key, value in overrides.items():
                if key not in valid:
                    raise ValueError(f"unknown config key: {key}")
                setattr(cfg, key, value)
        return cfg
