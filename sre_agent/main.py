"""Agent entrypoint: tail the lab's combined stream, run detection, and route confirmed
candidates through the Incident Manager (correlate → fingerprint → dedup → ticket).

Usage:
    python -m sre_agent.main                      # tail docker compose in the lab dir
    python -m sre_agent.main --stdin              # consume a piped/replayed stream
    python -m sre_agent.main --config overrides.json
    python -m sre_agent.main --maintenance        # suppress tickets/alerts (fault tests)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.changelog import ChangeLog
from sre_agent.config import Config
from sre_agent.detect.crashloop import CrashLoopDetector
from sre_agent.detect.engine import DetectionEngine
from sre_agent.detect.error_rate import ErrorRateDetector
from sre_agent.detect.latency import LatencyDetector
from sre_agent.detect.malformed import MalformedSpikeDetector
from sre_agent.detect.queue_depth import QueueDepthDetector
from sre_agent.detect.silence import SilenceDetector
from sre_agent.diagnosis.context import ContextAssembler
from sre_agent.diagnosis.diagnoser import Diagnoser
from sre_agent.diagnosis.llm import (FallbackProvider, GeminiProvider, LLMProvider,
                                     OpenRouterProvider)
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.ingest.parser import LineParser
from sre_agent.ingest.tailer import Tailer
from sre_agent.ingest.window import SlidingWindow
from sre_agent.integrations.notifications import ConsoleNotifier
from sre_agent.integrations.postmortems import SqlitePostMortemStore
from sre_agent.integrations.ticketing import SqliteTicketStore
from sre_agent.poll.adapters import build_live_pollers
from sre_agent.poll.store import PollCycle, PollLoop, SignalStore


def build_engine(cfg: Config, parser: LineParser, changelog=None) -> DetectionEngine:
    return DetectionEngine([
        ErrorRateDetector(cfg),
        LatencyDetector(cfg),
        SilenceDetector(cfg),
        CrashLoopDetector(cfg),
        QueueDepthDetector(cfg),
        MalformedSpikeDetector(cfg, parser),
    ], cfg, changelog=changelog)


def load_secrets(secrets_dir: str = ".secrets") -> None:
    """Load KEY=VALUE lines from every gitignored *.env file under .secrets/ into the env."""
    d = Path(secrets_dir)
    if not d.is_dir():
        return
    for path in sorted(d.glob("*.env")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def _atlassian_client():
    """A Jira/Confluence REST client if Atlassian credentials are present, else None."""
    site = os.environ.get("ATLASSIAN_SITE")
    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_API_TOKEN")
    if site and email and token:
        from sre_agent.integrations.jira import JiraClient
        return JiraClient(site, email, token)
    return None


def build_ticket_store(cfg: Config, data_dir: Path):
    local = SqliteTicketStore(data_dir / "tickets.db")   # always the local authoritative record
    if cfg.ticketing_backend == "jira":
        client = _atlassian_client()
        key = os.environ.get("JIRA_PROJECT_KEY")
        if client and key:
            from sre_agent.integrations.jira import JiraTicketStore
            from sre_agent.integrations.ticketing import CompositeTicketStore
            jira = JiraTicketStore(client, key, cfg.jira_oncall_account_id or None)
            return CompositeTicketStore(local, jira)   # SQLite + Jira, side by side
        print("# ticketing: jira requested but credentials/project missing — using sqlite only",
              file=sys.stderr)
    return local


def build_postmortem_store(cfg: Config, data_dir: Path):
    local = SqlitePostMortemStore(data_dir / "postmortems.db",
                                  markdown_dir=data_dir / "postmortems")
    if cfg.postmortems_backend == "confluence":
        client = _atlassian_client()
        space = os.environ.get("CONFLUENCE_SPACE_KEY")
        if client and space:
            from sre_agent.integrations.confluence import (CompositePostMortemStore,
                                                           ConfluencePublisher)
            return CompositePostMortemStore(local, ConfluencePublisher(client, space))
        print("# postmortems: confluence requested but credentials/space missing — local only",
              file=sys.stderr)
    return local


def _make_provider(name: str, cfg: Config) -> LLMProvider | None:
    """One concrete provider by name, or None if its API key isn't set."""
    if name == "openrouter":
        key = os.environ.get(cfg.openrouter_api_key_env)
        return OpenRouterProvider(api_key=key, model=cfg.openrouter_model,
                                  timeout_s=cfg.llm_timeout_s) if key else None
    if name == "gemini":
        key = os.environ.get(cfg.gemini_api_key_env)
        return GeminiProvider(api_key=key, model=cfg.gemini_model,
                              timeout_s=cfg.llm_timeout_s) if key else None
    return None


def build_provider(cfg: Config) -> LLMProvider | None:
    """Construct the configured LLM provider, or None if no API key is set (diagnosis is then
    skipped — the agent still detects, correlates, and tickets). With llm_fallback on, the
    other provider (if its key is also present) is chained behind the primary so a free-tier
    429 can't kill a live demo."""
    primary = _make_provider(cfg.llm_provider, cfg)
    if not cfg.llm_fallback:
        return primary
    secondary_name = "gemini" if cfg.llm_provider == "openrouter" else "openrouter"
    secondary = _make_provider(secondary_name, cfg)
    chain = [p for p in (primary, secondary) if p is not None]
    if not chain:
        return None
    return chain[0] if len(chain) == 1 else FallbackProvider(chain)


def _print_status(incident_store, parser, window, signals, cfg, now) -> None:
    """Heartbeat to stderr so the operator always knows the agent is alive and the true
    system state. 'healthy' is reserved for genuinely-healthy — a down container, a climbing
    queue, or an open (in-flight or escalated) incident all make it DEGRADED."""
    stamp = now.strftime("%H:%M:%S")
    active = incident_store.find_active()
    escalated = incident_store.find_by_state(IncidentState.ESCALATED)
    snap = signals.latest()
    down = [n for n, c in snap.containers.items() if c.status != "running"] if snap else []

    bits = [f"{parser.parsed_count} lines", f"{len(window.services())} services"]
    if snap is not None:
        bits.append("containers: " + ("all up" if not down else "DOWN=" + ",".join(down)))
        if snap.redis_queue_depth is not None:
            bits.append(f"queue={snap.redis_queue_depth}")

    degraded = bool(active) or bool(escalated) or bool(down)
    headline = "DEGRADED" if degraded else "healthy"
    print(f"# [{stamp}] {headline} · " + " · ".join(bits), file=sys.stderr)

    for inc in active:
        detail = ""
        if inc.state is IncidentState.VERIFYING and inc.verify_started_at is not None:
            left = cfg.recovery_window_s - (now - inc.verify_started_at).total_seconds()
            detail = f" — watching for recovery ({max(0, left):.0f}s left, else retry/escalate)"
        elif inc.state is IncidentState.AWAITING_APPROVAL and inc.approval_deadline is not None:
            left = (inc.approval_deadline - now).total_seconds()
            detail = (f" — needs approval: {inc.pending_action} {inc.pending_params} "
                      f"({max(0, left) / 60:.0f}m left). Run: python -m sre_agent.approve "
                      f"approve {inc.id} --by you --execute")
        print(f"#    {inc.id} [{inc.ticket_id}] {inc.fingerprint} → {inc.state.value}{detail}",
              file=sys.stderr)
    for inc in escalated:
        print(f"#    {inc.id} [{inc.ticket_id}] {inc.fingerprint} → ESCALATED — "
              f"handed to on-call, still open (agent took no further action)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args_parser = argparse.ArgumentParser(description="SRE agent (detection + incident mgmt)")
    args_parser.add_argument("--stdin", action="store_true",
                             help="read the log stream from stdin instead of docker")
    args_parser.add_argument("--config", help="JSON file with Config overrides")
    args_parser.add_argument("--no-poll", action="store_true",
                             help="disable docker/redis/pg secondary-signal polling")
    args_parser.add_argument("--maintenance", action="store_true",
                             help="maintenance mode: suppress all tickets and notifications")
    args_parser.add_argument("--execute", action="store_true",
                             help="actually execute remediation actions (default: dry-run)")
    args_parser.add_argument("--jira", action="store_true",
                             help="file tickets in Jira instead of the local SQLite stub")
    args_parser.add_argument("--confluence", action="store_true",
                             help="publish post-mortems to Confluence (in addition to local)")
    args = args_parser.parse_args(argv)

    # Windows consoles default to cp1252, which can't encode arrows/middots the agent prints —
    # force UTF-8 so notifications and status lines never crash the loop.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    cfg = Config.load(args.config)
    if args.maintenance:
        cfg.maintenance_mode = True
    if args.execute:
        cfg.dry_run = False
    if args.jira:
        cfg.ticketing_backend = "jira"
    if args.confluence:
        cfg.postmortems_backend = "confluence"
    window = SlidingWindow(cfg.window_max_age_s)
    parser = LineParser()
    tailer = Tailer(window, parser)
    signals = SignalStore()
    poll_loop: PollLoop | None = None

    data_dir = Path(cfg.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    load_secrets()
    provider = build_provider(cfg)
    changelog = ChangeLog(data_dir / "changes.db")
    postmortems = build_postmortem_store(cfg, data_dir)
    ticket_store = build_ticket_store(cfg, data_dir)
    engine = build_engine(cfg, parser, changelog=changelog)
    diagnoser = Diagnoser(provider, runs=cfg.diagnosis_runs, temperature=cfg.llm_temperature) \
        if provider is not None else None
    assembler = ContextAssembler(LAB_TOPOLOGY, cfg, changelog=changelog, postmortems=postmortems) \
        if provider is not None else None
    manager = IncidentManager(
        IncidentStore(data_dir / "incidents.db"),
        ticket_store,
        ConsoleNotifier(), LAB_TOPOLOGY, cfg,
        diagnoser=diagnoser, assembler=assembler,
        executor=ActionExecutor(dry_run=cfg.dry_run),
        guardrails=Guardrails(cfg.max_restarts_per_hour, changelog),
        recovery=RecoveryEvaluator(cfg),
        changelog=changelog, postmortems=postmortems,
    )
    print(f"# diagnosis: {'enabled (' + cfg.llm_provider + ')' if provider else 'disabled (no API key)'}; "
          f"actions: {'DRY-RUN' if cfg.dry_run else 'LIVE EXECUTE'}", file=sys.stderr)

    if args.stdin:
        tailer.start_stream(sys.stdin)
    else:
        tailer.start_docker(cfg.compose_dir)
        if not args.no_poll:
            poll_loop = PollLoop(PollCycle(build_live_pollers(cfg), signals), cfg.poll_interval_s)
            poll_loop.start()
    print(f"# sre-agent watching ({'stdin' if args.stdin else cfg.compose_dir}); "
          f"debounce={cfg.debounce_s:.0f}s correlation_window={cfg.correlation_window_s:.0f}s"
          f"{' [MAINTENANCE]' if cfg.maintenance_mode else ''}", file=sys.stderr)

    incident_store = manager._istore
    last_status = 0.0
    try:
        while True:
            time.sleep(cfg.tick_interval_s)
            now = datetime.now(timezone.utc)
            window.prune(now)
            for candidate in engine.tick(window, now):
                print(f"# candidate {candidate.fingerprint()} — {candidate.detail}", file=sys.stderr)
                manager.ingest(candidate, now)
            # one full agent cycle: create incidents, diagnose+act new ones, verify in-flight
            manager.step(window, signals.latest(), now)

            if time.monotonic() - last_status >= cfg.status_interval_s:
                _print_status(incident_store, parser, window, signals, cfg, now)
                last_status = time.monotonic()

            if not tailer.alive and not args.stdin:
                print("# log stream ended; exiting", file=sys.stderr)
                return 1
    except KeyboardInterrupt:
        return 0
    finally:
        tailer.stop()
        if poll_loop is not None:
            poll_loop.stop()


if __name__ == "__main__":
    sys.exit(main())
