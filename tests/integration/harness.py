"""Integration test harness: the REAL ingestion + detection engine + Incident Manager wired
together, driven by a synthetic JSON log stream over simulated time. No live lab, no network
— deterministic and fast — but it exercises the real components end to end, so it proves
things unit tests on hand-fed candidates cannot (e.g. that the real detectors emit the error
codes correlation depends on)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sre_agent.config import Config
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.notifications import InMemoryNotifier
from sre_agent.integrations.ticketing import SqliteTicketStore, Ticket
from sre_agent.ingest.parser import LineParser
from sre_agent.ingest.window import SlidingWindow
from sre_agent.main import build_engine

T0 = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


def integration_config() -> Config:
    """Short timings so a scenario plays out in seconds of simulated time, not minutes."""
    cfg = Config()
    cfg.tick_interval_s = 2.0
    cfg.debounce_s = 10.0
    cfg.grace_s = 6.0
    cfg.cooldown_s = 60.0
    cfg.error_rate_window_s = 30.0
    cfg.error_rate_threshold = 6
    cfg.silence_threshold_s = 15.0
    cfg.latency_min_samples = 8
    cfg.correlation_window_s = 6.0
    cfg.flap_window_s = 120.0
    cfg.recovery_window_s = 30.0
    cfg.recovery_check_s = 30.0
    cfg.max_remediation_loops = 2
    cfg.approval_timeout_s = 40.0
    return cfg


class PipelineHarness:
    def __init__(self, tmp_path, cfg: Config | None = None, *, diagnoser=None,
                 assembler=None, executor=None, guardrails=None, recovery=None,
                 changelog=None) -> None:
        self.cfg = cfg or integration_config()
        self.parser = LineParser()
        self.window = SlidingWindow(self.cfg.window_max_age_s)
        self.changelog = changelog
        self.engine = build_engine(self.cfg, self.parser, changelog=changelog)
        self.tickets = SqliteTicketStore(tmp_path / "t.db")
        self.incidents = IncidentStore(tmp_path / "i.db")
        self.notifier = InMemoryNotifier()
        self.manager = IncidentManager(self.incidents, self.tickets, self.notifier,
                                       LAB_TOPOLOGY, self.cfg, diagnoser=diagnoser,
                                       assembler=assembler, executor=executor,
                                       guardrails=guardrails, recovery=recovery,
                                       changelog=changelog)
        self.created: list[Incident] = []

    def advance_full(self, offset_s: float, signals=None) -> None:
        """Full agent cycle (detect → diagnose → act → verify), as main runs it."""
        now = T0 + timedelta(seconds=offset_s)
        self.window.prune(now)
        for candidate in self.engine.tick(self.window, now):
            self.manager.ingest(candidate, now)
        self.manager.step(self.window, signals, now)

    def emit(self, service: str, offset_s: float, *, level: str = "INFO", **fields) -> None:
        """Feed one JSON log line (as the real tailer would) at T0+offset_s."""
        ts = (T0 + timedelta(seconds=offset_s)).isoformat()
        line = json.dumps({"ts": ts, "service": service, "level": level, **fields})
        rec = self.parser.parse(line, now=T0 + timedelta(seconds=offset_s))
        if rec is not None:
            self.window.append(rec)

    def advance_to(self, offset_s: float) -> list[Incident]:
        """Run one detection+manager tick at T0+offset_s; return any incidents acted on."""
        now = T0 + timedelta(seconds=offset_s)
        self.window.prune(now)
        for candidate in self.engine.tick(self.window, now):
            self.manager.ingest(candidate, now)
        acted = self.manager.tick(now)
        self.created.extend(i for i in acted if i not in self.created)
        return acted

    def open_tickets(self) -> list[Ticket]:
        return self.tickets.list_open()
