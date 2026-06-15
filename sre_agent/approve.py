"""Human approval CLI for Tier-2 incidents (M5). Operates on the agent's persisted state, so
an operator can approve/reject a proposal the running agent posted.

    python -m sre_agent.approve list
    python -m sre_agent.approve approve INC-3 --by alice@example.com
    python -m sre_agent.approve reject  INC-3 --by alice@example.com
    python -m sre_agent.approve resolve INC-3 --by alice@example.com   # close one I fixed by hand
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.changelog import ChangeLog
from sre_agent.config import Config
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.manager import IncidentManager
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.notifications import ConsoleNotifier
from sre_agent.integrations.ticketing import SqliteTicketStore


def _manager(cfg: Config) -> tuple[IncidentManager, IncidentStore]:
    data_dir = Path(cfg.data_dir)
    incidents = IncidentStore(data_dir / "incidents.db")
    changelog = ChangeLog(data_dir / "changes.db")
    mgr = IncidentManager(
        incidents, SqliteTicketStore(data_dir / "tickets.db"), ConsoleNotifier(),
        LAB_TOPOLOGY, cfg,
        executor=ActionExecutor(dry_run=cfg.dry_run),
        guardrails=Guardrails(cfg.max_restarts_per_hour, changelog),
        recovery=RecoveryEvaluator(cfg), changelog=changelog,
    )
    return mgr, incidents


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Approve or reject Tier-2 incident actions")
    ap.add_argument("command", choices=["list", "approve", "reject", "resolve"])
    ap.add_argument("incident_id", nargs="?", help="e.g. INC-3")
    ap.add_argument("--by", default="operator", help="who is approving/rejecting/resolving")
    ap.add_argument("--config", help="JSON config overrides")
    ap.add_argument("--execute", action="store_true",
                    help="actually run the approved action (default: dry-run)")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    if args.execute:
        cfg.dry_run = False
    mgr, incidents = _manager(cfg)
    now = datetime.now(timezone.utc)

    if args.command == "list":
        pending = incidents.find_by_state(IncidentState.AWAITING_APPROVAL)
        if not pending:
            print("no incidents awaiting approval")
        for inc in pending:
            print(f"{inc.id} [{inc.ticket_id}] {inc.fingerprint}: "
                  f"proposed {inc.pending_action} {inc.pending_params} "
                  f"(deadline {inc.approval_deadline})")
        return 0

    if not args.incident_id:
        ap.error("incident_id is required for approve/reject")
    if args.command == "approve":
        result = mgr.approve(args.incident_id, approver=args.by, now=now)
        if result is None:
            print(f"{args.incident_id} is not awaiting approval")
            return 1
        print(f"approved {args.incident_id}: {'ok' if result.success else 'FAILED'} — {result.detail}")
    elif args.command == "resolve":
        inc = mgr.manual_resolve(args.incident_id, resolver=args.by, now=now)
        if inc is None:
            print(f"{args.incident_id} is not human-owned (escalated/flapping) — nothing to "
                  f"close (agent-driven incidents resolve themselves on recovery)")
            return 1
        print(f"resolved {args.incident_id} → RESOLVED (closed by {args.by})")
    else:
        mgr.reject(args.incident_id, approver=args.by, now=now)
        print(f"rejected {args.incident_id} → escalated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
