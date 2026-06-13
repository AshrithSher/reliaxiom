"""TDD: the LLM provider boundary. Diagnosis talks only to LLMProvider, so Gemini-now /
Anthropic-later is a mechanical swap. Transport (HTTP) is injected, so request shaping and
response parsing are tested without a network call."""
import json

import pytest

from sre_agent.diagnosis.llm import GeminiProvider, LLMError, LLMProvider

# a minimal real-shaped Gemini response
GEMINI_OK = json.dumps({
    "candidates": [{"content": {"parts": [{"text": "root cause: redis down"}]}}]
})


def test_complete_returns_model_text():
    calls = {}

    def transport(url, api_key, body, timeout):
        calls["url"], calls["api_key"], calls["body"] = url, api_key, body
        return GEMINI_OK

    provider = GeminiProvider(api_key="k123", model="gemini-flash-latest", transport=transport)
    out = provider.complete(system="you are an SRE", user="what broke?")
    assert out == "root cause: redis down"


def test_request_shaping():
    captured = {}

    def transport(url, api_key, body, timeout):
        captured["url"], captured["api_key"] = url, api_key
        captured["payload"] = json.loads(body)
        return GEMINI_OK

    GeminiProvider(api_key="secret", model="gemini-flash-latest",
                   transport=transport).complete(system="SYS", user="USR", temperature=0.2)
    assert "gemini-flash-latest:generateContent" in captured["url"]
    assert captured["api_key"] == "secret"
    assert captured["payload"]["contents"][0]["parts"][0]["text"] == "USR"
    assert captured["payload"]["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert captured["payload"]["generationConfig"]["temperature"] == 0.2


def test_blocked_or_empty_response_raises():
    provider = GeminiProvider(api_key="k", transport=lambda *a: json.dumps({"candidates": []}))
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_malformed_transport_output_raises():
    provider = GeminiProvider(api_key="k", transport=lambda *a: "not json")
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_transport_error_is_wrapped():
    def boom(*a):
        raise OSError("connection reset")
    provider = GeminiProvider(api_key="k", transport=boom, sleep=lambda _: None)
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")


def test_retries_transient_then_succeeds():
    import urllib.error
    attempts = {"n": 0}

    def flaky(url, api_key, body, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.HTTPError(url, 429, "rate limited", {}, None)
        return GEMINI_OK

    provider = GeminiProvider(api_key="k", transport=flaky, retries=3, sleep=lambda _: None)
    assert provider.complete(system="s", user="u") == "root cause: redis down"
    assert attempts["n"] == 3


def test_does_not_retry_non_transient():
    import urllib.error
    attempts = {"n": 0}

    def auth_fail(url, api_key, body, timeout):
        attempts["n"] += 1
        raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)

    provider = GeminiProvider(api_key="k", transport=auth_fail, retries=3, sleep=lambda _: None)
    with pytest.raises(LLMError):
        provider.complete(system="s", user="u")
    assert attempts["n"] == 1   # 403 is not transient — no retry


# --- OpenRouter provider (OpenAI-compatible chat completions) --------------------
OPENROUTER_OK = json.dumps({"choices": [{"message": {"content": "root cause: redis down"}}]})


def test_openrouter_returns_message_content():
    from sre_agent.diagnosis.llm import OpenRouterProvider
    out = OpenRouterProvider(api_key="k", transport=lambda *a: OPENROUTER_OK).complete(
        system="sys", user="usr")
    assert out == "root cause: redis down"


def test_openrouter_request_shaping():
    from sre_agent.diagnosis.llm import OpenRouterProvider
    captured = {}

    def transport(url, api_key, body, timeout):
        captured["url"], captured["api_key"], captured["payload"] = url, api_key, json.loads(body)
        return OPENROUTER_OK

    OpenRouterProvider(api_key="secret", model="x/y:free", transport=transport).complete(
        system="SYS", user="USR", temperature=0.3)
    assert "openrouter.ai/api/v1/chat/completions" in captured["url"]
    assert captured["api_key"] == "secret"
    p = captured["payload"]
    assert p["model"] == "x/y:free"
    assert p["messages"] == [{"role": "system", "content": "SYS"},
                             {"role": "user", "content": "USR"}]
    assert p["temperature"] == 0.3


def test_openrouter_retries_then_succeeds():
    import urllib.error
    from sre_agent.diagnosis.llm import OpenRouterProvider
    n = {"i": 0}

    def flaky(url, api_key, body, timeout):
        n["i"] += 1
        if n["i"] < 2:
            raise urllib.error.HTTPError(url, 429, "rate", {}, None)
        return OPENROUTER_OK

    out = OpenRouterProvider(api_key="k", transport=flaky, retries=3, sleep=lambda _: None).complete(
        system="s", user="u")
    assert out == "root cause: redis down" and n["i"] == 2


# --- FallbackProvider (tries providers in order; a free-tier 429 can't kill a live demo) ---
class _StubProvider(LLMProvider):
    """A provider that either returns a fixed answer or raises, recording its calls."""

    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.calls = 0

    def complete(self, *, system, user, temperature=0.0):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.answer


def test_fallback_uses_first_when_it_succeeds():
    from sre_agent.diagnosis.llm import FallbackProvider
    primary = _StubProvider(answer="primary says redis down")
    secondary = _StubProvider(answer="secondary")
    out = FallbackProvider([primary, secondary]).complete(system="s", user="u")
    assert out == "primary says redis down"
    assert primary.calls == 1 and secondary.calls == 0   # secondary never consulted


def test_fallback_falls_through_to_next_on_error():
    from sre_agent.diagnosis.llm import FallbackProvider
    primary = _StubProvider(error=LLMError("429 rate limited"))
    secondary = _StubProvider(answer="secondary saved the demo")
    out = FallbackProvider([primary, secondary]).complete(system="s", user="u")
    assert out == "secondary saved the demo"
    assert primary.calls == 1 and secondary.calls == 1


def test_fallback_passes_through_temperature_and_args():
    from sre_agent.diagnosis.llm import FallbackProvider
    captured = {}

    class _Capture(LLMProvider):
        def complete(self, *, system, user, temperature=0.0):
            captured.update(system=system, user=user, temperature=temperature)
            return "ok"

    FallbackProvider([_Capture()]).complete(system="SYS", user="USR", temperature=0.7)
    assert captured == {"system": "SYS", "user": "USR", "temperature": 0.7}


def test_fallback_raises_when_all_fail():
    from sre_agent.diagnosis.llm import FallbackProvider
    p1 = _StubProvider(error=LLMError("boom1"))
    p2 = _StubProvider(error=LLMError("boom2"))
    with pytest.raises(LLMError):
        FallbackProvider([p1, p2]).complete(system="s", user="u")
    assert p1.calls == 1 and p2.calls == 1


def test_fallback_requires_at_least_one_provider():
    from sre_agent.diagnosis.llm import FallbackProvider
    with pytest.raises(ValueError):
        FallbackProvider([])
