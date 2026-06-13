"""LLM provider boundary for the diagnosis layer.

Diagnosis depends only on `LLMProvider.complete(system, user) -> text`. Implementations:
`GeminiProvider` (Google AI Studio) and `OpenRouterProvider` (OpenAI-compatible, any model).
Swapping providers is config-only. HTTP transport is injected so request shaping and response
parsing are unit-testable without a network call; transient failures (429/5xx) retry with
exponential backoff.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Callable

# transport(url, api_key, body, timeout) -> response text
Transport = Callable[[str, str, str, float], str]


class LLMError(Exception):
    """Any failure to obtain a usable completion (transport, blocked, or malformed)."""


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str: ...


class GeminiProvider(LLMProvider):
    _BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str = "gemini-flash-latest",
                 timeout_s: float = 30.0, transport: Transport | None = None,
                 retries: int = 3, backoff_s: float = 2.0, sleep: Callable[[float], None] | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s
        self._transport = transport or _urllib_post
        self._retries = retries
        self._backoff = backoff_s
        self._sleep = sleep or time.sleep

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        payload = {
            "contents": [{"parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {"temperature": temperature},
        }
        url = f"{self._BASE}/{self._model}:generateContent"
        raw = _request_with_retry(self._transport, url, self._api_key, json.dumps(payload),
                                  self._timeout, self._retries, self._backoff, self._sleep)
        try:
            data = json.loads(raw)
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unusable response: {raw[:200]!r}") from exc


class OpenRouterProvider(LLMProvider):
    """OpenAI-compatible chat completions via OpenRouter — works with any model OpenRouter
    serves (incl. free tiers). Bearer auth; response is choices[0].message.content."""

    _URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "google/gemma-4-31b-it:free",
                 timeout_s: float = 45.0, transport: Transport | None = None,
                 retries: int = 4, backoff_s: float = 2.0,
                 sleep: Callable[[float], None] | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s
        self._transport = transport or _openrouter_post
        self._retries = retries
        self._backoff = backoff_s
        self._sleep = sleep or time.sleep

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
        }
        raw = _request_with_retry(self._transport, self._URL, self._api_key, json.dumps(payload),
                                  self._timeout, self._retries, self._backoff, self._sleep)
        try:
            return json.loads(raw)["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unusable response: {raw[:200]!r}") from exc


class FallbackProvider(LLMProvider):
    """Try each provider in order, moving to the next on any LLMError. Lets a live demo
    survive a free-tier rate-limit (429) on the primary by falling through to a secondary —
    each underlying provider has already exhausted its own transient-retry budget, so an
    LLMError here means that provider is genuinely unavailable, not merely flaky."""

    def __init__(self, providers: list[LLMProvider]) -> None:
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self._providers = providers

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        last: LLMError | None = None
        for provider in self._providers:
            try:
                return provider.complete(system=system, user=user, temperature=temperature)
            except LLMError as exc:
                last = exc
        raise LLMError(f"all {len(self._providers)} providers failed; last: {last}") from last


def _request_with_retry(transport: Transport, url: str, api_key: str, body: str, timeout: float,
                        retries: int, backoff: float, sleep: Callable[[float], None]) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return transport(url, api_key, body, timeout)
        except Exception as exc:  # noqa: BLE001 — retry only on transient codes
            last = exc
            if not _is_transient(exc) or attempt == retries - 1:
                break
            sleep(backoff * (2 ** attempt))   # exponential backoff
    raise LLMError(f"transport failed after {retries} attempts: {last}") from last


def _is_transient(exc: Exception) -> bool:
    """Rate-limit (429) and server (5xx) errors are worth retrying; 4xx auth/format aren't."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def _urllib_post(url: str, api_key: str, body: str, timeout: float) -> str:
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _openrouter_post(url: str, api_key: str, body: str, timeout: float) -> str:
    req = urllib.request.Request(
        url, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                 "X-Title": "sre-agent"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")
