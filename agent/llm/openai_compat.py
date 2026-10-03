"""
Adapter for any provider that speaks the OpenAI Chat Completions wire format:
OpenRouter, OpenAI, Groq, Together, a local Ollama/vLLM server, and so on.
One adapter covers all of them — they differ only in base URL, key, and
model id, which is what PRESETS below captures.

Talks to the endpoint with plain httpx rather than a vendor SDK: the request
is a single POST, and one small adapter with no extra dependency is easier to
reason about than an SDK per provider.

The model must support tool calling. Vision is optional: if the model
rejects images, run discovery with --no-vision.
"""
from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Optional

import httpx

from agent.llm.base import (
    LLMConfigError,
    LLMError,
    LLMProvider,
    LLMResponse,
    RetryableLLMError,
    TextBlock,
    ToolUseBlock,
    block_attr,
)
from agent.tools import TOOLS

# name -> (base URL, env var prefix, default model or None if one must be set)
PRESETS: dict[str, tuple[str, str, Optional[str]]] = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER", "google/gemini-2.5-flash"),
    "openai": ("https://api.openai.com/v1", "OPENAI", "gpt-4o-mini"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ", None),
    # Anything else OpenAI-compatible: set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.
    "openai_compat": ("", "LLM", None),
}

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
REQUEST_TIMEOUT_S = 60.0


def to_openai_messages(system_prompt: str, messages: list[dict],
                       image_bytes: Optional[bytes] = None,
                       image_mime_type: str = "image/png") -> list[dict]:
    """Neutral history (see agent/llm/base.py) -> Chat Completions messages."""
    out: list[dict] = [{"role": "system", "content": system_prompt}]
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            out.append({"role": msg["role"], "content": content})
            continue

        if msg["role"] == "assistant":
            text = "".join(block_attr(b, "text", "") for b in content if block_attr(b, "type") == "text")
            tool_calls = [
                {"id": block_attr(b, "id"), "type": "function",
                 "function": {"name": block_attr(b, "name"),
                              "arguments": json.dumps(block_attr(b, "input") or {})}}
                for b in content if block_attr(b, "type") == "tool_use"
            ]
            entry: dict = {"role": "assistant", "content": text or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue

        for block in content:
            if block_attr(block, "type") == "tool_result":
                out.append({"role": "tool", "tool_call_id": block_attr(block, "tool_use_id"),
                            "content": str(block_attr(block, "content", ""))})
            elif block_attr(block, "type") == "text":
                out.append({"role": "user", "content": block_attr(block, "text", "")})

    if image_bytes:
        # Only the current screenshot is sent, never the history's. A `tool`
        # message can't carry an image, so it goes in a user message: merged
        # into the latest one if that is what the history ends with,
        # otherwise appended after the tool result it illustrates.
        image_part = {"type": "image_url", "image_url": {
            "url": f"data:{image_mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"}}
        if out[-1]["role"] == "user" and isinstance(out[-1]["content"], str):
            out[-1] = {"role": "user", "content": [{"type": "text", "text": out[-1]["content"]}, image_part]}
        else:
            out.append({"role": "user", "content": [
                {"type": "text", "text": "Current screenshot of the page:"}, image_part]})
    return out


def to_openai_tools() -> list[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in TOOLS]


class OpenAICompatProvider(LLMProvider):
    def __init__(self, name: str, base_url: str, api_key: str, model: str,
                 client: Optional[httpx.Client] = None):
        self.name = name
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_S)

    @classmethod
    def from_env(cls, name: str) -> "OpenAICompatProvider":
        base_url, prefix, default_model = PRESETS[name]
        base_url = os.environ.get(f"{prefix}_BASE_URL") or base_url
        api_key = os.environ.get(f"{prefix}_API_KEY")
        model = os.environ.get(f"{prefix}_MODEL") or default_model
        missing = [var for var, val in ((f"{prefix}_BASE_URL", base_url), (f"{prefix}_API_KEY", api_key),
                                        (f"{prefix}_MODEL", model)) if not val]
        if missing:
            raise LLMConfigError(f"Provider '{name}' needs {', '.join(missing)} to be set.")
        return cls(name, base_url, api_key, model)

    def decide(self, system_prompt: str, messages: list[dict],
               image_bytes: Optional[bytes] = None,
               image_mime_type: str = "image/png") -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": to_openai_messages(system_prompt, messages, image_bytes, image_mime_type),
            "tools": to_openai_tools(),
            "tool_choice": "auto",
            "max_tokens": 2048,
        }
        try:
            resp = self._client.post(self._url, json=payload,
                                     headers={"Authorization": f"Bearer {self._api_key}"})
        except httpx.TransportError as e:  # timeouts, refused/reset connections, DNS
            raise RetryableLLMError(f"{self.name}: {type(e).__name__}: {e}") from e

        if resp.status_code != 200:
            self._raise_for(resp.status_code, resp.text, resp.headers.get("retry-after"))

        try:
            body = resp.json()
        except ValueError as e:
            raise RetryableLLMError(f"{self.name}: response was not JSON: {resp.text[:200]!r}") from e

        # OpenRouter can report an upstream failure as HTTP 200 with an
        # `error` object instead of `choices`.
        if not body.get("choices"):
            err = body.get("error") or {}
            code = err.get("code")
            self._raise_for(code if isinstance(code, int) else 502, json.dumps(err or body)[:300], None)

        return self._parse(body["choices"][0].get("message") or {})

    def _raise_for(self, status: int, detail: str, retry_after: Optional[str]) -> None:
        message = f"{self.name}: HTTP {status}: {detail[:300]}"
        if status in RETRYABLE_STATUS:
            try:
                retry_after_s = float(retry_after) if retry_after else None
            except ValueError:
                retry_after_s = None
            raise RetryableLLMError(message, retry_after_s=retry_after_s)
        raise LLMError(message)

    def _parse(self, message: dict) -> LLMResponse:
        blocks: list = []
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            blocks.append(TextBlock(text, provider=self.name))
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = None
            if not isinstance(args, dict):
                # Unparseable arguments: surface as text so the loop
                # re-prompts, rather than acting on a guess.
                blocks.append(TextBlock(f"(tool call {fn.get('name')!r} had malformed arguments)",
                                        provider=self.name))
                continue
            blocks.append(ToolUseBlock(call.get("id") or uuid.uuid4().hex, fn.get("name", ""), args,
                                       provider=self.name))
        if not blocks:
            blocks = [TextBlock("(model returned no content this turn)", provider=self.name)]
        return LLMResponse(blocks, provider=self.name, model=self.model)
