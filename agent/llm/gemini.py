"""
Adapter for the Gemini API (`google-genai`, `client.models.generate_content`
with manual function calling).

Gemini-specific wrinkles this adapter carries so nothing else has to:

  - "Thinking" models attach an opaque `thought_signature` to the Part that
    carries a function_call, and reject a follow-up request whose history has
    that function_call without its signature
    (https://ai.google.dev/gemini-api/docs/thinking#signatures). The
    signature rides along on the neutral block as `provider_data` and is
    echoed back unchanged. It is never inspected or generated here.
  - A tool call made by a DIFFERENT provider earlier in the run (after a
    failover) has no signature Gemini would accept. Those turns are sent as
    plain text instead of function_call/function_response parts, so the
    model still sees what happened without the API rejecting the history.
  - Gemini function calls have no call id; one is minted per call so the
    loop's tool_use_id / tool_result pairing works.
  - Vision: the current screenshot is attached to the latest user-role turn
    only. Re-sending every prior screenshot each turn would grow token cost
    for no benefit.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Optional

from agent.llm.base import (
    LLMConfigError,
    LLMError,
    LLMProvider,
    LLMResponse,
    RetryableLLMError,
    TextBlock,
    ToolUseBlock,
    block_attr,
    find_tool_use,
)
from agent.tools import TOOLS

NAME = "gemini"
DEFAULT_MODEL = "gemini-3.8-flash"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _strip_unsupported_schema_keys(schema: Any) -> Any:
    """Gemini's schema validator rejects "default" (used by wait_for_text's
    timeout_ms). The loop's own .get(..., 8000) still supplies the default
    when the model omits it."""
    if isinstance(schema, dict):
        return {k: _strip_unsupported_schema_keys(v) for k, v in schema.items() if k != "default"}
    if isinstance(schema, list):
        return [_strip_unsupported_schema_keys(v) for v in schema]
    return schema


def _is_native(block: Any) -> bool:
    """True if this block can be replayed to Gemini as a real function_call
    (it came from Gemini, or from a test double with no provider set)."""
    return block_attr(block, "provider") in (None, NAME)


class GeminiProvider(LLMProvider):
    name = NAME

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        # Imported here so a deployment that only uses an OpenAI-compatible
        # provider never needs the Google SDK importable.
        from google import genai
        from google.genai import errors, types

        self._types = types
        self._api_error = errors.APIError
        self._client = genai.Client(api_key=api_key)
        self.model = model
        self._function_declarations = [
            {"name": t["name"], "description": t["description"],
             "parameters": _strip_unsupported_schema_keys(t["input_schema"])}
            for t in TOOLS
        ]

    @classmethod
    def from_env(cls) -> "GeminiProvider":
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise LLMConfigError("Provider 'gemini' needs GEMINI_API_KEY to be set.")
        return cls(api_key, os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL)

    def decide(self, system_prompt: str, messages: list[dict],
               image_bytes: Optional[bytes] = None,
               image_mime_type: str = "image/png") -> LLMResponse:
        types = self._types
        contents = self._to_contents(messages, image_bytes, image_mime_type)
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[types.Tool(function_declarations=self._function_declarations)],
                    max_output_tokens=2048,
                ),
            )
        except self._api_error as e:
            message = f"gemini: {e}"
            if getattr(e, "code", None) in RETRYABLE_STATUS:
                raise RetryableLLMError(message) from e
            raise LLMError(message) from e
        except Exception as e:  # noqa: BLE001 - transport-level failure (timeout, reset, DNS)
            raise RetryableLLMError(f"gemini: {type(e).__name__}: {e}") from e
        return self._to_response(response)

    def _to_contents(self, messages: list[dict], image_bytes: Optional[bytes],
                     image_mime_type: str) -> list:
        types = self._types
        contents = []
        last_user_idx: Optional[int] = None
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            content = msg["content"]
            parts = []

            if isinstance(content, str):
                parts.append(types.Part(text=content))
            else:
                for block in content:
                    block_type = block_attr(block, "type")
                    if block_type == "text":
                        sig = block_attr(block, "provider_data") if _is_native(block) else None
                        parts.append(types.Part(text=block_attr(block, "text"), thought_signature=sig))

                    elif block_type == "tool_use":
                        name, args = block_attr(block, "name"), block_attr(block, "input") or {}
                        if _is_native(block):
                            parts.append(types.Part(
                                function_call=types.FunctionCall(name=name, args=args),
                                thought_signature=block_attr(block, "provider_data"),
                            ))
                        else:
                            parts.append(types.Part(text=f"[called tool {name} with {json.dumps(args)}]"))

                    elif block_type == "tool_result":
                        call = find_tool_use(messages, block_attr(block, "tool_use_id"))
                        result_text = str(block_attr(block, "content", ""))
                        if call is not None and _is_native(call):
                            parts.append(types.Part(function_response=types.FunctionResponse(
                                name=block_attr(call, "name"), response={"result": result_text},
                            )))
                        else:
                            parts.append(types.Part(text=f"[tool result] {result_text}"))

            if parts:
                contents.append(types.Content(role=role, parts=parts))
                if role == "user":
                    last_user_idx = len(contents) - 1

        if image_bytes and last_user_idx is not None:
            contents[last_user_idx].parts.append(
                types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type)
            )
        return contents

    def _to_response(self, response) -> LLMResponse:
        candidates = getattr(response, "candidates", None) or []
        if not candidates or not candidates[0].content or not candidates[0].content.parts:
            # Empty response (safety block, or nothing produced). A text
            # block makes the loop re-prompt, which is the right recovery.
            return LLMResponse([TextBlock("(model returned no content this turn)", provider=NAME)],
                               provider=NAME, model=self.model)

        blocks: list = []
        for part in candidates[0].content.parts:
            sig = getattr(part, "thought_signature", None)
            text = getattr(part, "text", None)
            if text:
                blocks.append(TextBlock(text, provider=NAME, provider_data=sig))
                continue
            fc = getattr(part, "function_call", None)
            if fc is not None:
                blocks.append(ToolUseBlock(uuid.uuid4().hex, fc.name, dict(fc.args) if fc.args else {},
                                           provider=NAME, provider_data=sig))

        if not blocks:
            blocks = [TextBlock("(model returned no usable content this turn)", provider=NAME)]
        return LLMResponse(blocks, provider=NAME, model=self.model)
