"""TDD: the Diagnoser. The LLM proposes; code disposes. The model returns structured JSON
(root cause, evidence, a catalog action id) — it NEVER sets a tier or a routing confidence.
Trust guards force escalation when: evidence is hallucinated, two runs disagree, the action
is invalid, or the output is malformed.
"""
import json

from sre_agent.action.catalog import Tier
from sre_agent.diagnosis.diagnoser import Diagnoser


def response(action="restart_container", params=None, evidence=None, root="redis is down"):
    return json.dumps({
        "root_cause": root,
        "evidence": evidence if evidence is not None else ["req abc123 hit 503"],
        "selected_action": action,
        "action_params": params if params is not None else {"service": "worker"},
        "alternative_hypotheses": ["network blip"],
    })


class FakeProvider:
    """Returns canned responses in sequence; repeats the last once exhausted."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = 0

    def complete(self, *, system, user, temperature=0.0):
        i = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[i]


ALWAYS = lambda ref: True
NEVER = lambda ref: False


def test_happy_path_auto_action():
    d = Diagnoser(FakeProvider([response()]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert out.selected_action == "restart_container"
    assert out.tier is Tier.AUTO
    assert not out.escalate
    assert out.root_cause == "redis is down"


def test_approval_action_keeps_tier_not_escalated():
    d = Diagnoser(FakeProvider([response(params={"service": "redis"})]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert out.tier is Tier.APPROVAL and not out.escalate


def test_hallucinated_evidence_escalates():
    d = Diagnoser(FakeProvider([response()]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=NEVER)
    assert out.escalate and out.tier is Tier.ESCALATE
    assert any("evidence" in r.lower() for r in out.escalation_reasons)


def test_disagreeing_runs_escalate():
    d = Diagnoser(FakeProvider([response(action="restart_container"),
                                response(action="flush_queue", params={})]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert out.escalate
    assert any("disagree" in r.lower() for r in out.escalation_reasons)


def test_invalid_action_escalates():
    d = Diagnoser(FakeProvider([response(action="delete_database", params={})]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert out.escalate and out.tier is Tier.ESCALATE


def test_malformed_json_escalates():
    d = Diagnoser(FakeProvider(["I think redis is down, sorry no JSON"]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert out.escalate


def test_markdown_fenced_json_is_parsed():
    fenced = "```json\n" + response() + "\n```"
    d = Diagnoser(FakeProvider([fenced]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert not out.escalate and out.selected_action == "restart_container"


def test_parses_json_wrapped_in_prose():
    noisy = "Sure! Here is my analysis:\n```json\n" + response() + "\n```\nHope that helps."
    d = Diagnoser(FakeProvider([noisy]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert not out.escalate and out.selected_action == "restart_container"


def test_parses_json_with_brace_in_string_value():
    resp = json.dumps({"root_cause": "config had a stray } brace", "evidence": ["rid-1"],
                       "selected_action": "restart_container", "action_params": {"service": "api"},
                       "alternative_hypotheses": []})
    d = Diagnoser(FakeProvider(["prefix " + resp + " suffix"]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert not out.escalate and out.action_params == {"service": "api"}


def test_single_run_skips_agreement_check():
    d = Diagnoser(FakeProvider([response()]), runs=1)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert not out.escalate
    assert d.provider.calls == 1


# --- restart_target: the service to restart is the correlated root, not the model's guess ---
def test_restart_target_overrides_model_service_to_root():
    # model proposes restarting a stateless *symptom* service; the authoritative root is
    # stateful. The tier must follow the real target (APPROVAL), not the model's pick (AUTO).
    d = Diagnoser(FakeProvider([response(action="restart_container", params={"service": "api"})]),
                  runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS, restart_target="postgres")
    assert out.action_params["service"] == "postgres"
    assert out.tier is Tier.APPROVAL
    assert not out.escalate


def test_restart_target_fills_missing_service():
    # model omits the service entirely — previously an "invalid action" escalation; now the
    # authoritative root fills it in.
    d = Diagnoser(FakeProvider([response(action="restart_container", params={})]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS, restart_target="redis")
    assert out.action_params["service"] == "redis"
    assert out.tier is Tier.APPROVAL and not out.escalate


def test_restart_target_resolves_cross_run_service_disagreement():
    # two runs name different symptom services; pinned to the root they agree → no escalation.
    d = Diagnoser(FakeProvider([response(action="restart_container", params={"service": "api"}),
                                response(action="restart_container", params={"service": "worker"})]),
                  runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS, restart_target="postgres")
    assert not out.escalate
    assert out.action_params["service"] == "postgres"


def test_restart_target_leaves_non_restart_actions_untouched():
    d = Diagnoser(FakeProvider([response(action="flush_queue", params={})]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS, restart_target="postgres")
    assert out.selected_action == "flush_queue"
    assert "service" not in out.action_params


def test_no_restart_target_preserves_model_service():
    # backward-compatible: without a target the model's service still stands.
    d = Diagnoser(FakeProvider([response(action="restart_container", params={"service": "api"})]),
                  runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert out.action_params["service"] == "api"
    assert out.tier is Tier.AUTO


def test_action_id_with_param_suffix_is_normalized():
    # the model echoed the catalog's display form "restart_container(service)" as the id —
    # a real, fixable action must not be rejected as "unknown action" and escalated.
    d = Diagnoser(FakeProvider([response(action="restart_container(service)")]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS)
    assert out.selected_action == "restart_container"
    assert out.tier is Tier.AUTO and not out.escalate


def test_action_id_suffix_normalized_still_pins_restart_target():
    # the parenthetical artifact must be stripped *before* the root-service pinning kicks in
    d = Diagnoser(FakeProvider([response(action="restart_container(service)",
                                         params={"service": "api"})]), runs=2)
    out = d.diagnose(system="s", user="u", verify_evidence=ALWAYS, restart_target="postgres")
    assert out.selected_action == "restart_container"
    assert out.action_params["service"] == "postgres"
    assert out.tier is Tier.APPROVAL and not out.escalate
