"""Integration eval for the P0 action-layer work (D-037/D-038): the SAME kill-worker
scenario that M4 proved on docker now runs end-to-end through the **Kubernetes** ActionBackend
and the **atomic rate limiter** — proving the new SPIs don't perturb the engine (the P0.1
exit criterion) and that remediation goes via a declarative k8s rollout, not a docker socket.

Plus the fleet-safety property the old per-process cap lacked: two limiters over one shared
store (standing in for two replicas) share a single cap."""
import json

from sre_agent.action.executor import ActionExecutor, Guardrails
from sre_agent.action.k8s_backend import KubernetesActionBackend
from sre_agent.action.ratelimit import SqliteRateLimiter
from sre_agent.action.recovery import RecoveryEvaluator
from sre_agent.changelog import ChangeLog
from sre_agent.diagnosis.context import ContextAssembler
from sre_agent.diagnosis.diagnoser import Diagnoser
from sre_agent.incident.lifecycle import IncidentState
from sre_agent.incident.topology import LAB_TOPOLOGY
from sre_agent.integrations.ticketing import TicketStatus
from tests.integration.harness import T0, PipelineHarness, integration_config

ALL = ["gateway", "webapp", "api", "worker", "loadgen"]


class FakeProvider:
    def __init__(self, response):
        self._response = response

    def complete(self, *, system, user, temperature=0.0):
        return self._response


class FakeKube:
    """Records (method, path); every call succeeds (HTTP 200), as a healthy API would."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, path, body):
        self.calls.append((method, path))
        return 200, "{}"


RESTART_WORKER = json.dumps({
    "root_cause": "the worker has hung and stopped processing",
    "evidence": ["the incident header shows root_service=worker with fault=silence"],
    "selected_action": "restart_container",
    "action_params": {"service": "worker"},
    "alternative_hypotheses": [],
})


def test_kill_worker_remediated_via_kubernetes_backend(tmp_path):
    cfg = integration_config()
    changelog = ChangeLog(tmp_path / "c.db")
    kube = FakeKube()
    h = PipelineHarness(
        tmp_path, cfg,
        diagnoser=Diagnoser(FakeProvider(RESTART_WORKER), runs=2),
        assembler=ContextAssembler(LAB_TOPOLOGY, cfg),
        executor=ActionExecutor(
            backend=KubernetesActionBackend(transport=kube, namespace="lab"), dry_run=False),
        guardrails=Guardrails(),
        recovery=RecoveryEvaluator(cfg),
        changelog=changelog,
    )
    t = 0
    while t <= 90:
        for svc in ALL:
            if svc == "worker" and 20 < t < 58:    # worker silent, then back post-restart
                continue
            h.emit(svc, t, level="INFO", event="request", status=200, request_id=f"ok{int(t)}")
        h.advance_full(t)
        t += 2

    resolved = h.incidents.find_by_state(IncidentState.RESOLVED)
    assert len(resolved) == 1                                   # exactly one incident, resolved
    assert resolved[0].fingerprint == "worker:silence"
    assert h.tickets.get(resolved[0].ticket_id).status is TicketStatus.RESOLVED
    # remediation went through the k8s API as a Deployment rollout — not a docker socket
    assert ("PATCH", "/apis/apps/v1/namespaces/lab/deployments/worker") in kube.calls
    # and the agent tagged its own action exactly once (invariant #5; no self-induced incident)
    from datetime import timedelta
    tagged = [e for e in changelog.recent(T0 - timedelta(seconds=1), T0 + timedelta(seconds=200))
              if e.actor == "sre-agent" and e.service == "worker"]
    assert len(tagged) == 1
    assert h.tickets.get("SRE-2") is None


def test_two_replicas_share_one_restart_cap(tmp_path):
    """The fleet-safety property: the cap is a property of the shared store, not the process.
    Two limiters on one DB (two replicas) cannot collectively exceed the cap."""
    db = tmp_path / "changes.db"
    ChangeLog(db)                       # create + WAL once, as the agent does at startup
    a = SqliteRateLimiter(max_restarts_per_hour=3, path=db)
    b = SqliteRateLimiter(max_restarts_per_hour=3, path=db)
    from tests.integration.harness import T0 as NOW
    assert a.try_consume("restart_container", "worker", NOW).allowed
    assert b.try_consume("restart_container", "worker", NOW).allowed
    assert a.try_consume("restart_container", "worker", NOW).allowed   # 3rd, at cap
    blocked = b.try_consume("restart_container", "worker", NOW)        # 4th, across replicas
    assert not blocked.allowed and "cap" in blocked.reason.lower()
