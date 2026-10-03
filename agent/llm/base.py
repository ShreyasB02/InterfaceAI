"""
The provider-neutral contract the discovery loop talks to.

discovery_loop.py keeps one running `messages` history in a neutral shape
and never knows which vendor is behind it:

    {"role": "user",      "content": "<text>"}
    {"role": "assistant", "content": [TextBlock | ToolUseBlock, ...]}
    {"role": "user",      "content": [{"type": "tool_result",
                                       "tool_use_id": "...", "content": "<text>"}]}

Each provider adapter translates that history into its own wire format on
every call and translates the reply back into TextBlock/ToolUseBlock. Because
the history is neutral, the router can fail over to a different provider
mid-run and the new one picks up the same conversation.

Two error classes are the whole retry contract:

  RetryableLLMError - worth trying again (rate limit, 5xx, overload, timeout,
                      dropped connection). The router backs off and retries,
                      then fails over.
  LLMError          - not worth retrying on this provider (bad key, unknown
                      model, malformed request). The router skips straight to
                      the next provider.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class LLMError(Exception):
    """A provider call failed in a way retrying the same provider won't fix."""


class RetryableLLMError(LLMError):
    """A transient provider failure. `retry_after_s` is the provider's own
    hint (e.g. a Retry-After header), when it gave one."""

    def __init__(self, message: str, retry_after_s: Optional[float] = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LLMConfigError(LLMError):
    """No usable provider is configured (missing key, unknown provider name)."""


@dataclass
class TextBlock:
    text: str
    type: str = field(default="text", init=False)
    # Opaque provider-specific data that must be echoed back unchanged on the
    # next call to the SAME provider (Gemini's thought_signature). Never
    # inspected here, never sent to a different provider.
    provider: Optional[str] = None
    provider_data: Any = None


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = field(default="tool_use", init=False)
    provider: Optional[str] = None
    provider_data: Any = None


@dataclass
class LLMResponse:
    content: list
    provider: str = "unknown"
    model: str = "unknown"


class LLMProvider(ABC):
    """One vendor adapter. Stateless across calls: everything it needs is in
    the neutral `messages` history it is handed."""

    name: str
    model: str

    @abstractmethod
    def decide(self, system_prompt: str, messages: list[dict],
               image_bytes: Optional[bytes] = None,
               image_mime_type: str = "image/png") -> LLMResponse:
        """One observe -> decide call. Raises RetryableLLMError or LLMError."""


# -- helpers for reading the neutral history ---------------------------------
# Blocks are normally TextBlock/ToolUseBlock objects, but a plain dict of the
# same shape is accepted too, so a test double doesn't have to import these.

def block_attr(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def find_tool_use(messages: list[dict], tool_use_id: str) -> Any:
    """The assistant tool_use block a tool_result answers, or None."""
    for msg in messages:
        if msg["role"] != "assistant" or isinstance(msg["content"], str):
            continue
        for block in msg["content"]:
            if block_attr(block, "type") == "tool_use" and block_attr(block, "id") == tool_use_id:
                return block
    return None
