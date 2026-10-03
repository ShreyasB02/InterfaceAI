"""Provider-agnostic LLM access for the discovery loop. See base.py for the
neutral message contract and router.py for retry/failover policy."""
from agent.llm.base import (
    LLMConfigError,
    LLMError,
    LLMProvider,
    LLMResponse,
    RetryableLLMError,
    TextBlock,
    ToolUseBlock,
)
from agent.llm.router import LLMClient

__all__ = [
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "RetryableLLMError",
    "TextBlock",
    "ToolUseBlock",
]
