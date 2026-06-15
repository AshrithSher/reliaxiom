"""The Incident Manager — owner of the incident lifecycle and the only component that talks
to ticketing/notifications. Buffers candidates over the correlation window, collapses a
cascade into one incident (one fault = one ticket), dedups recurrences into comments,
reopens flapping incidents, escalates chronic flappers, and stays silent in maintenance."""
from __future__ import annotations

from datetime import datetime, timedelta

from typing import TYPE_CHECKING

from sre_agent.action.catalog import Tier, resolve
from sre_agent.changelog import ChangeLogEntry
from sre_agent.config import Config
from sre_agent.incident.correlation import CorrelationGroup, Correlator
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.models import Incident
from sre_agent.incident.store import IncidentStore
from sre_agent.incident.topology import TopologyMap
from sre_agent.integrations.notifications import Notification, Notifier
from sre_agent.integrations.ticketing import TicketStatus, TicketStore
from sre_agent.models import IncidentCandidate

if TYPE_CHECKING:
    from datetime import datetime as _dt  # noqa
    from sre_agent.diagnosis.context import ContextAssembler
    from sre_agent.diagnosis.diagnoser import Diagnoser
    from sre_agent.diagnosis.schema import Diagnosis
    from sre_agent.ingest.window import SlidingWindow

_ONCALL = "oncall@example.com"

# canonical legal walk to RESOLVED from any open state (M2 helper standing in for the M4
# verification loop)
_RESOLVE_PATH: dict[IncidentState, list[IncidentState]] = {
    IncidentState.DETECTED: [IncidentState.DIAGNOSING, IncidentState.VERIFYING,
                             IncidentState.RESOLVED],
    IncidentState.DIAGNOSING: [IncidentState.VERIFYING, IncidentState.RESOLVED],
    IncidentState.AWAITING_APPROVAL: [IncidentState.ACTING, IncidentState.VERIFYING,
                                      IncidentState.RESOLVED],
    IncidentState.ACTING: [IncidentState.VERIFYING, IncidentState.RESOLVED],
    IncidentState.VERIFYING: [IncidentState.RESOLVED],
    IncidentState.FLAPPING: [IncidentState.DIAGNOSING, IncidentState.VERIFYING,
                             IncidentState.RESOLVED],
}


class IncidentManager:
    def __init__(self, incident_store: IncidentStore, ticket_store: TicketStore,
                 notifier: Notifier, topology: TopologyMap, cfg: Config, *,
                 diagnoser: "Diagnoser | None" = None,
                 assembler: "ContextAssembler | None" = None,
                 executor=None, guardrails=None, recovery=None, changelog=None,
                 postmortems=None) -> None:
        self._istore = incident_store
        self._tickets = ticket_store
        self._notifier = notifier
        self._topology = topology
        self._correlator = Correlator(topology)
        self._cfg = cfg
        self._diagnoser = diagnoser
        self._assembler = assembler
        self._executor = executor
        self._guardrails = guardrails
        self._recovery = recovery
        self._changelog = changelog
        self._postmortems = postmortems
        self._pending: list[tuple[datetime, IncidentCandidate]] = []

    # --- ingestion / time --------------------------------------------------------
    def ingest(self, candidate: IncidentCandidate, now: datetime) -> None:
        self._pending.append((now, candidate))

    def tick(self, now: datetime) -> list[Incident]:
        """Flush matured candidates once the oldest has aged past the correlation window,
        correlate the burst, and create/dedup/reopen incidents."""
        if not self._pending:
            return []
        if self._cfg.maintenance_mode:
            self._pending.clear()   # collected but suppressed — total silence
            return []
        oldest = min(ts for ts, _ in self._pending)
        if (now - oldest).total_seconds() < self._cfg.correlation_window_s:
            return []   # still collecting the cascade
        batch = [c for _, c in self._pending]
        self._pending.clear()

        results: list[Incident] = []
        for group in self._correlator.correlate(batch):
            inc = self._handle_group(group, now)
            if inc is not None:
                results.append(inc)
        return results

    # --- diagnosis (M3) ----------------------------------------------------------
    def diagnose(self, incident: Incident, window: "SlidingWindow", now: "_dt",
                 signals=None) -> "Diagnosis | None":
        """Run the LLM diagnosis for a DETECTED incident, comment it, and either park the
        incident in DIAGNOSING (a real action was proposed) or escalate (a guard tripped).
        No-op if no diagnoser/assembler was wired (preserves M2 behavior)."""
        if self._diagnoser is None or self._assembler is None:
            return None
        system, user = self._assembler.build(incident, window, now, signals=signals)
        # anti-fabrication: a citation is "grounded" if it refers to anything in the prompt
        # we actually gave the model (logs, incident header, signals) — not only log records.
        # A made-up request id still won't appear; citing provided context is not a hallucination.
        verifier = self._assembler.make_verifier(system + "\n" + user)
        # the restart target is the correlated root_service, decided by code — never the
        # model's symptom-service guess (keeps a stateful Tier-2 restart from downgrading)
        diagnosis = self._diagnoser.diagnose(system=system, user=user, verify_evidence=verifier,
                                             restart_target=incident.root_service)

        if incident.state is IncidentState.DETECTED:
            incident.transition(IncidentState.DIAGNOSING, now)   # may be a re-diagnose (retry)

        # transient LLM outage (429/timeout) — retry on a later cycle rather than give up,
        # so the agent self-heals when the provider comes back. Escalate only after a cap.
        if diagnosis.provider_unavailable:
            incident.diagnosis_attempts += 1
            if incident.diagnosis_attempts >= self._cfg.max_diagnosis_attempts:
                self._escalate(incident, now,
                               f"LLM unavailable after {incident.diagnosis_attempts} attempts")
            else:
                self._comment(incident, f"diagnosis deferred — LLM unavailable "
                              f"(attempt {incident.diagnosis_attempts}/"
                              f"{self._cfg.max_diagnosis_attempts}), will retry", now)
                self._istore.save(incident)
            return diagnosis

        incident.root_cause = diagnosis.root_cause   # captured for the post-mortem
        self._comment(
            incident,
            f"diagnosis: {diagnosis.root_cause} → proposed {diagnosis.selected_action} "
            f"{diagnosis.action_params} (tier {diagnosis.tier.name})", now)

        if diagnosis.escalate:
            self._escalate(incident, now, "; ".join(diagnosis.escalation_reasons))
        else:
            self._istore.save(incident)
        return diagnosis

    # --- full-lifecycle orchestration (M4) ---------------------------------------
    def step(self, window, signals, now: "_dt") -> None:
        """One agent cycle: create incidents from matured candidates, diagnose+act new
        ones, advance verifications, and re-diagnose retries. Used by both main and the
        integration harness so they exercise the same path."""
        for inc in self.tick(now):
            if inc.state is IncidentState.DETECTED:       # tick may also return dedup'd
                self._progress(inc, window, signals, now)  # existing incidents — skip those
        for inc in self._istore.find_by_state(IncidentState.VERIFYING):
            self.verify_incident(inc, window, signals, now)
        for inc in self._istore.find_by_state(IncidentState.DIAGNOSING):
            self._progress(inc, window, signals, now)   # retry after a failed verification
        self.check_approval_timeouts(now)               # Tier-2 waits that ran out

    def _progress(self, incident: Incident, window, signals, now: "_dt"):
        if self._diagnoser is None or self._assembler is None:
            return None
        diagnosis = self.diagnose(incident, window, now, signals=signals)
        if diagnosis is None or diagnosis.provider_unavailable or diagnosis.escalate:
            return diagnosis   # transient (retry next cycle) or already escalated
        # optional policy (D-021): a never-seen fault with no prior post-mortem can be held
        # for human review rather than auto-acted. Only on the first attempt (not retries).
        if self._cfg.escalate_unknown_fingerprint and not incident.action_taken \
                and self._is_novel(incident):
            self._escalate(incident, now,
                           "novel fault — no prior post-mortem; held for human review")
            return diagnosis
        if diagnosis.tier is Tier.AUTO and self._executor is not None:
            self.act(incident, diagnosis, now)
        elif diagnosis.tier is Tier.APPROVAL:
            self.request_approval(incident, diagnosis, now)   # Tier 2 → wait for a human
        else:
            self._escalate(incident, now, f"action tier {diagnosis.tier.name} not actionable")
        return diagnosis

    # --- act + verify (M4) -------------------------------------------------------
    def act(self, incident: Incident, diagnosis, now: "_dt"):
        """Execute a Tier-1 AUTO action under guardrails, tag it in the change log, and
        enter VERIFYING. A blocked guardrail (cap reached, non-AUTO tier) escalates."""
        action = resolve(diagnosis.selected_action, diagnosis.action_params)
        decision = self._guardrails.check(action, now)
        if not decision.allowed:
            self._escalate(incident, now, f"guardrail blocked {action.action_id}: {decision.reason}")
            return None
        target = action.params.get("service") or incident.root_service
        incident.transition(IncidentState.ACTING, now)
        # tag BEFORE executing, so detection ignores the transient our action causes (#5)
        self._changelog.record(ChangeLogEntry(ts=now, actor="sre-agent", service=target,
                                              change_type=action.action_id,
                                              detail=str(action.params)))
        result = self._executor.execute(action, now)
        incident.action_taken = f"{action.action_id} {action.params}"
        self._comment(incident, f"action taken: {action.action_id} {action.params} → "
                      f"{'ok' if result.success else 'FAILED'} ({result.detail})", now)
        incident.transition(IncidentState.VERIFYING, now)
        incident.verify_started_at = now
        self._comment(incident, f"verifying recovery — watching up to "
                      f"{self._cfg.recovery_window_s:.0f}s for the fault to clear", now)
        self._istore.save(incident)
        return result

    # --- HITL approval (M5) ------------------------------------------------------
    def request_approval(self, incident: Incident, diagnosis, now: "_dt") -> None:
        """Tier-2: post a proposal with blast radius and wait. No action runs yet."""
        action = resolve(diagnosis.selected_action, diagnosis.action_params)
        incident.transition(IncidentState.AWAITING_APPROVAL, now)
        incident.pending_action = action.action_id
        incident.pending_params = action.params
        incident.approval_deadline = now + timedelta(seconds=self._cfg.approval_timeout_s)
        target = action.params.get("service") or incident.root_service
        blast = sorted({target} | self._topology.dependents_of(target))
        self._comment(incident, f"APPROVAL REQUIRED: {action.action_id} {action.params} — "
                      f"blast radius {blast}. Diagnosis: {diagnosis.root_cause}", now)
        if incident.ticket_id:
            self._tickets.set_status(incident.ticket_id, TicketStatus.AWAITING_APPROVAL, now=now)
        self._notify("approval", incident,
                     f"approve {action.action_id} on {target}? blast radius: {blast}")
        self._istore.save(incident)

    def approve(self, incident_id: str, approver: str, now: "_dt"):
        inc = self._istore.get(incident_id)
        if inc is None or inc.state is not IncidentState.AWAITING_APPROVAL:
            return None
        action = resolve(inc.pending_action or "", inc.pending_params)
        target = action.params.get("service") or inc.root_service
        inc.transition(IncidentState.ACTING, now)
        self._changelog.record(ChangeLogEntry(ts=now, actor="sre-agent", service=target,
                                              change_type=action.action_id,
                                              detail=f"approved by {approver}"))
        result = self._executor.execute(action, now)
        inc.action_taken = f"{action.action_id} {action.params} (approved by {approver})"
        if inc.ticket_id:
            self._tickets.add_comment(
                inc.ticket_id, author=approver,
                body=f"APPROVED by {approver}: {action.action_id} {action.params} — "
                     f"{'ok' if result.success else 'FAILED'}: {result.detail}", now=now)
        inc.transition(IncidentState.VERIFYING, now)
        inc.verify_started_at = now
        inc.pending_action = None
        inc.approval_deadline = None
        self._comment(inc, f"verifying recovery — watching up to "
                      f"{self._cfg.recovery_window_s:.0f}s for the fault to clear", now)
        self._istore.save(inc)
        return result

    def reject(self, incident_id: str, approver: str, now: "_dt") -> None:
        inc = self._istore.get(incident_id)
        if inc is None or inc.state is not IncidentState.AWAITING_APPROVAL:
            return
        self._escalate(inc, now, f"action rejected by {approver}")

    def check_approval_timeouts(self, now: "_dt") -> None:
        for inc in self._istore.find_by_state(IncidentState.AWAITING_APPROVAL):
            if inc.approval_deadline is not None and now >= inc.approval_deadline:
                self._escalate(inc, now, "approval timed out — no action taken")

    def verify_incident(self, incident: Incident, window, signals, now: "_dt") -> str:
        """One verification step. recovered → RESOLVED; window elapsed without recovery →
        re-diagnose (bounded by max_remediation_loops) → ESCALATED; else keep waiting."""
        if self._recovery.recovered(incident, window, signals, now):
            self.resolve(incident.id, now, comment="verified recovery")
            return "resolved"
        started = incident.verify_started_at or incident.updated_at
        if (now - started).total_seconds() >= self._cfg.recovery_window_s:
            incident.remediation_loops += 1
            if incident.remediation_loops >= self._cfg.max_remediation_loops:
                self._escalate(incident, now, "no recovery after remediation attempts")
                return "escalated"
            incident.transition(IncidentState.DIAGNOSING, now)
            self._comment(incident, f"not recovered in {self._cfg.recovery_window_s:.0f}s — "
                          f"re-diagnosing (loop {incident.remediation_loops})", now)
            self._istore.save(incident)
            return "retry"
        return "waiting"

    def _is_novel(self, incident: Incident) -> bool:
        """True if we have no prior post-mortem for this fingerprint (never seen this fault)."""
        return (self._postmortems is not None
                and not self._postmortems.recent_for_fingerprint(incident.fingerprint, limit=1))

    def _escalate(self, incident: Incident, now: "_dt", reason: str) -> None:
        incident.transition(IncidentState.ESCALATED, now)
        if incident.ticket_id:
            self._tickets.set_status(incident.ticket_id, TicketStatus.ESCALATED, now=now)
            self._tickets.assign(incident.ticket_id, _ONCALL, now=now)
            self._tickets.add_comment(incident.ticket_id, author="agent",
                                      body="escalating: " + reason, now=now)
        self._notify("escalated", incident, reason)
        self._istore.save(incident)
        self._write_postmortem(incident, "escalated", now)

    # --- resolution (M2 helper; M4 replaces with the verification loop) -----------
    def resolve(self, incident_id: str, now: datetime,
                comment: str = "verified recovery") -> Incident | None:
        inc = self._istore.get(incident_id)
        if inc is None:
            return None
        for state in _RESOLVE_PATH[inc.state]:
            inc.transition(state, now)
        self._istore.save(inc)
        if inc.ticket_id:
            self._tickets.set_status(inc.ticket_id, TicketStatus.RESOLVED, now=now)
            self._tickets.add_comment(inc.ticket_id, author="agent", body=comment, now=now)
        self._notify("resolved", inc, comment)
        self._write_postmortem(inc, "resolved", now)
        return inc

    def manual_resolve(self, incident_id: str, resolver: str, now: "_dt") -> Incident | None:
        """Human closure for an incident the agent handed off. ESCALATED (rejected / timed out
        / guardrail / chronic flap) and FLAPPING incidents are human-owned: the agent never
        auto-closes them (that's why a manual `docker start redis` after a reject doesn't move
        the ticket on its own). This is the person who fixed it out-of-band telling the system
        'I fixed it, close it.' Agent-driven incidents still resolve through the verify loop —
        this path is only for the states the agent has stepped away from, so it returns None
        (a no-op) for anything else rather than racing the agent's own lifecycle."""
        inc = self._istore.get(incident_id)
        if inc is None:
            return None
        if inc.state not in (IncidentState.ESCALATED, IncidentState.FLAPPING):
            return None
        inc.transition(IncidentState.RESOLVED, now)
        self._istore.save(inc)
        if inc.ticket_id:
            self._tickets.set_status(inc.ticket_id, TicketStatus.RESOLVED, now=now)
            self._tickets.add_comment(
                inc.ticket_id, author=resolver,
                body=f"manually resolved by {resolver} — fixed out-of-band, closed by a human",
                now=now)
        self._notify("resolved", inc, f"manually resolved by {resolver}")
        self._write_postmortem(inc, "resolved", now)
        return inc

    def _write_postmortem(self, inc: Incident, outcome: str, now: "_dt") -> None:
        """Capture what happened for the record and as incident memory (M6)."""
        if self._postmortems is None:
            return
        ttr = ((inc.resolved_at or now) - inc.first_seen).total_seconds() \
            if outcome == "resolved" else None
        timeline = [f"{inc.first_seen.isoformat()} detected {inc.fingerprint}"]
        if inc.root_cause:
            timeline.append(f"diagnosed: {inc.root_cause}")
        if inc.action_taken:
            timeline.append(f"action: {inc.action_taken}")
        timeline.append(f"{now.isoformat()} {outcome}")
        from sre_agent.integrations.postmortems import PostMortem
        self._postmortems.record(PostMortem(
            incident_id=inc.id, ticket_id=inc.ticket_id, fingerprint=inc.fingerprint,
            root_service=inc.root_service, services=inc.services,
            root_cause=inc.root_cause or "undetermined",
            action_taken=inc.action_taken or "none", outcome=outcome,
            time_to_resolve_s=ttr, created_at=now, timeline=timeline))

    # --- group handling ----------------------------------------------------------
    def _handle_group(self, group: CorrelationGroup, now: datetime) -> Incident | None:
        fp = group.fingerprint

        open_inc = self._istore.find_open_by_fingerprint(fp)
        if open_inc is not None:
            self._comment(open_inc, f"recurring symptom for {fp}: {self._summary(group)}", now)
            return open_inc

        resolved = self._istore.find_latest_resolved(fp)
        if resolved is not None and resolved.resolved_at is not None and \
                (now - resolved.resolved_at).total_seconds() <= self._cfg.flap_window_s:
            return self._reopen(resolved, group, now)

        return self._create(group, now)

    def _create(self, group: CorrelationGroup, now: datetime) -> Incident:
        severity = self._severity(group)
        first_seen = min(c.first_seen for c in group.candidates)
        ticket = self._tickets.create(
            fingerprint=group.fingerprint, title=self._title(group), services=group.services,
            fault_type=group.fault_type, severity=severity,
            evidence=self._initial_report(group, severity, first_seen, now), now=now,
        )
        inc = Incident(
            id=ticket.id.replace("SRE-", "INC-"), fingerprint=group.fingerprint,
            ticket_id=ticket.id, state=IncidentState.DETECTED, root_service=group.root_service,
            services=group.services, fault_type=group.fault_type, severity=severity,
            first_seen=first_seen, updated_at=now,
        )
        self._istore.save(inc)
        self._notify("created", inc, self._summary(group))
        return inc

    def _reopen(self, inc: Incident, group: CorrelationGroup, now: datetime) -> Incident:
        inc.transition(IncidentState.FLAPPING, now)
        inc.flap_count += 1
        if inc.ticket_id:
            self._tickets.set_status(inc.ticket_id, TicketStatus.OPEN, now=now)
            self._tickets.add_comment(
                inc.ticket_id, author="agent",
                body=f"recurred within flap window — reopened (flap #{inc.flap_count})", now=now)
        kind = "reopened"
        if inc.flap_count >= self._cfg.flap_escalate_after:
            inc.transition(IncidentState.ESCALATED, now)
            if inc.ticket_id:
                self._tickets.set_status(inc.ticket_id, TicketStatus.ESCALATED, now=now)
                self._tickets.assign(inc.ticket_id, _ONCALL, now=now)
                self._tickets.add_comment(inc.ticket_id, author="agent",
                                          body="flapping repeatedly — escalating to on-call",
                                          now=now)
            kind = "escalated"
        self._istore.save(inc)
        self._notify(kind, inc, f"recurrence of {group.fingerprint}")
        return inc

    # --- helpers -----------------------------------------------------------------
    def _comment(self, inc: Incident, body: str, now: datetime) -> None:
        if inc.ticket_id:
            self._tickets.add_comment(inc.ticket_id, author="agent", body=body, now=now)

    def _notify(self, kind: str, inc: Incident, body: str) -> None:
        self._notifier.post(Notification(
            kind=kind, incident_id=inc.id, ticket_id=inc.ticket_id, severity=inc.severity,
            title=f"{inc.root_service} {inc.fault_type}", body=body[:200]))

    def _severity(self, group: CorrelationGroup) -> str:
        if "loadgen" in group.services:
            return "High"   # the synthetic user is seeing errors → user-facing
        if "worker" in group.services or group.fault_type in {"silence", "queue_growth"}:
            return "Medium"  # background lag
        return "Low"        # degraded but serving

    @staticmethod
    def _title(group: CorrelationGroup) -> str:
        return f"{group.root_service} {group.fault_type} ({', '.join(group.services)})"

    @staticmethod
    def _initial_report(group: CorrelationGroup, severity: str, first_seen: "_dt",
                        now: "_dt") -> str:
        """The full initial incident report — goes in the ticket description at creation;
        everything after is added as comments as the incident progresses."""
        evidence = "\n".join(f"- {c.detail}" for c in group.candidates)
        return (
            "SRE agent incident — initial report\n"
            f"Fingerprint: {group.fingerprint}\n"
            f"Suspected root: {group.root_service}\n"
            f"Affected services: {', '.join(group.services)}\n"
            f"Severity: {severity}\n"
            f"Fault type: {group.fault_type}\n"
            f"First detected: {first_seen.isoformat()}\n"
            f"Ticket opened: {now.isoformat()}\n\n"
            f"Detection evidence:\n{evidence}\n\n"
            "The SRE agent will post diagnosis, actions taken, and verification results as "
            "comments below as it works this incident."
        )

    @staticmethod
    def _summary(group: CorrelationGroup) -> str:
        return "; ".join(c.detail for c in group.candidates)[:1000]
