"""
`LLMClient`: what the discovery loop holds. An ordered list of providers with
bounded retry and failover, behind the same `decide()` every provider has.

Policy, in one place:

  - A transient failure (RetryableLLMError: rate limit, 5xx/overload,
    timeout) is retried on the same provider with exponential backoff, up to
    `max_retries` extra attempts, honouring the provider's Retry-After hint.
  - When retries run out, or the failure isn't retryable at all (bad key,
    unknown model), the client fails over to the next provider in the list.
  - Failover is sticky: once a provider has been abandoned it is not tried
    again this run. A run never ping-pongs between two half-working vendors.
  - When every provider is exhausted, one LLMError lists what each said.

Configuration (see .env.example):

  LLM_PROVIDERS   ordered, comma-separated, e.g. "openrouter,gemini".
                  Unset: every provider whose API key is present, in the
                  order of KNOWN_PROVIDERS.
  LLM_MAX_RETRIES extra attempts per provider on a transient failure
                  (default 3).

Replay never imports this package.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from agent.llm.base import LLMConfigError, LLMError, LLMProvider, LLMResponse, RetryableLLMError
from agent.llm.gemini import GeminiProvider
from agent.llm.openai_compat import PRESETS, OpenAICompatProvider

KNOWN_PROVIDERS = ["gemini", *PRESETS]
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 20.0

EventSink = Callable[[dict], None]


def build_provider(name: str) -> LLMProvider:
    if name == "gemini":
        return GeminiProvider.from_env()
    if name in PRESETS:
        return OpenAICompatProvider.from_env(name)
    raise LLMConfigError(f"Unknown LLM provider '{name}'. Known: {', '.join(KNOWN_PROVIDERS)}.")


def _has_key(name: str) -> bool:
    if name == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    return bool(os.environ.get(f"{PRESETS[name][1]}_API_KEY"))


class LLMClient:
    def __init__(self, providers: list[LLMProvider], max_retries: int = DEFAULT_MAX_RETRIES,
                 on_event: Optional[EventSink] = None, sleep: Callable[[float], None] = time.sleep):
        if not providers:
            raise LLMConfigError("LLMClient needs at least one provider.")
        self._providers = providers
        self._active = 0
        self._max_retries = max_retries
        self._sleep = sleep
        # Set by the discovery run so retries and failovers land in its
        # evidence log, not just on stderr.
        self.on_event: EventSink = on_event or (lambda event: None)

    @classmethod
    def from_env(cls) -> "LLMClient":
        raw = os.environ.get("LLM_PROVIDERS", "")
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
        if not names:
            names = [n for n in KNOWN_PROVIDERS if _has_key(n)]
        if not names:
            raise LLMConfigError(
                "No LLM provider is configured. Set an API key (GEMINI_API_KEY, OPENROUTER_API_KEY, "
                "OPENAI_API_KEY, GROQ_API_KEY, or LLM_API_KEY + LLM_BASE_URL + LLM_MODEL), and "
                "optionally LLM_PROVIDERS to choose the order. See .env.example."
            )
        # A provider that is listed but misconfigured fails here, at startup,
        # not ten turns into a run when it is first needed as a fallback.
        providers = [build_provider(n) for n in names]
        return cls(providers, max_retries=int(os.environ.get("LLM_MAX_RETRIES", DEFAULT_MAX_RETRIES)))

    # The provider currently serving calls — what artifact provenance records.
    @property
    def provider(self) -> str:
        return self._providers[self._active].name

    @property
    def model(self) -> str:
        return self._providers[self._active].model

    def describe(self) -> str:
        return " -> ".join(f"{p.name}:{p.model}" for p in self._providers)

    def decide(self, system_prompt: str, messages: list[dict],
               image_bytes: Optional[bytes] = None, image_mime_type: str = "image/png") -> LLMResponse:
        errors: list[str] = []
        while self._active < len(self._providers):
            provider = self._providers[self._active]
            try:
                response = self._call_with_retry(provider, system_prompt, messages, image_bytes, image_mime_type)
            except LLMError as e:
                errors.append(str(e))
                next_idx = self._active + 1
                next_name = self._providers[next_idx].name if next_idx < len(self._providers) else None
                self.on_event({"event": "llm_failover", "from_provider": provider.name,
                               "to_provider": next_name, "error": str(e)[:300]})
                self._active = next_idx
                continue
            return self._single_tool_call(response)
        raise LLMError("All configured LLM providers failed: " + " | ".join(errors))

    def _call_with_retry(self, provider: LLMProvider, system_prompt: str, messages: list[dict],
                         image_bytes: Optional[bytes], image_mime_type: str) -> LLMResponse:
        attempt = 0
        while True:
            try:
                return provider.decide(system_prompt, messages, image_bytes=image_bytes,
                                       image_mime_type=image_mime_type)
            except RetryableLLMError as e:
                attempt += 1
                if attempt > self._max_retries:
                    raise
                delay = min(e.retry_after_s or BACKOFF_BASE_S * 2 ** (attempt - 1), BACKOFF_CAP_S)
                self.on_event({"event": "llm_retry", "provider": provider.name, "attempt": attempt,
                               "delay_s": delay, "error": str(e)[:300]})
                self._sleep(delay)

    @staticmethod
    def _single_tool_call(response: LLMResponse) -> LLMResponse:
        """The loop acts on one tool call per turn. If a model emits several
        in parallel, keep the first: an unanswered tool call left in the
        history makes the next request invalid on every provider."""
        seen_tool = False
        kept = []
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                if seen_tool:
                    continue
                seen_tool = True
            kept.append(block)
        response.content = kept
        return response
