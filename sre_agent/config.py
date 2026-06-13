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

    # Integration backends (M7). Default to real Atlassian (Jira + Confluence); the composite
    # stores keep a local SQLite/markdown mirror and degrade to it if Atlassian is unreachable
    # or its credentials are missing, so this default is safe even offline.
    ticketing_backend: str = "jira"        # "jira" | "sqlite"
    postmortems_backend: str = "confluence"  # "confluence" | "local"
    jira_oncall_account_id: str = ""       # Atlassian accountId to assign escalations to

    # Sliding window
    window_max_age_s: int = 900  # keep 15 min of context for later layers

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
