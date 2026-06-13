"""TDD: the action catalog. The LLM only ever picks an action id; the TIER is decided here,
in code (invariant #2). A confident-but-wrong model must never be able to mark a risky
action auto-executable, and an unknown action or bad params must force escalation."""
from sre_agent.action.catalog import CATALOG, Tier, catalog_summary, resolve


def test_restart_stateless_is_auto():
    for svc in ["webapp", "api", "worker", "gateway", "auth", "payments", "loadgen"]:
        r = resolve("restart_container", {"service": svc})
        assert r.valid and r.tier is Tier.AUTO


def test_every_lab_service_is_restartable():
    """Guard: every service in the topology must be a known restart target, else an incident
    rooted there escalates with 'unknown service' (D-029 pins the restart to the root)."""
    from sre_agent.action.catalog import STATEFUL_SERVICES, STATELESS_SERVICES
    from sre_agent.incident.topology import LAB_TOPOLOGY

    known = STATELESS_SERVICES | STATEFUL_SERVICES
    for svc in LAB_TOPOLOGY._direct:   # noqa: SLF001 — test reaches into the map intentionally
        assert svc in known, f"{svc} is in the topology but not restartable in the catalog"


def test_restart_stateful_requires_approval():
    for svc in ["redis", "postgres"]:
        r = resolve("restart_container", {"service": svc})
        assert r.valid and r.tier is Tier.APPROVAL


def test_restart_unknown_service_escalates():
    r = resolve("restart_container", {"service": "mystery"})
    assert not r.valid and r.tier is Tier.ESCALATE


def test_queue_actions_tiers():
    assert resolve("clear_stuck_queue_item", {"item_id": "x"}).tier is Tier.AUTO
    assert resolve("rerun_failed_job", {"job_id": "j"}).tier is Tier.AUTO
    assert resolve("flush_queue", {}).tier is Tier.APPROVAL


def test_change_config_requires_approval():
    r = resolve("change_config", {"service": "api", "key": "timeout", "value": "5"})
    assert r.valid and r.tier is Tier.APPROVAL


def test_unknown_action_escalates():
    r = resolve("delete_database", {})
    assert not r.valid and r.tier is Tier.ESCALATE
    assert "unknown" in r.reason.lower()


def test_missing_params_escalates():
    r = resolve("restart_container", {})  # no service
    assert not r.valid and r.tier is Tier.ESCALATE
    assert "param" in r.reason.lower()


def test_llm_cannot_supply_its_own_tier():
    # extra keys (like an attempted tier override) are ignored — tier comes from code only
    r = resolve("restart_container", {"service": "redis", "tier": "auto"})
    assert r.tier is Tier.APPROVAL


def test_catalog_summary_does_not_glue_id_to_params():
    # the prompt rendering must not show "restart_container(service)" — that display form
    # tempted the model to copy the whole token as the action id and self-escalate.
    summary = catalog_summary()
    for spec in CATALOG.values():
        assert spec.id in summary
        assert f"{spec.id}(" not in summary
