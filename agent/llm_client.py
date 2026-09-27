"""Thin wrapper around the Gemini API for the discovery loop's
observe -> decide step. Kept separate from discovery_loop.py so the loop
itself is easy to read, and so a different provider could be swapped in
by implementing the same decide() signature.

This was originally written against Anthropic's Messages API, whose
response shape is a flat list of content blocks (`{type: "text" | "tool_use",
...}`) that discovery_loop.py reads directly (`response.content`,
`block.type`, `block.text`, `tool_use.name`, `tool_use.input`, `tool_use.id`)
and also re-appends verbatim into the running `messages` history.

Gemini's SDK (`google-genai`) has a different response shape
(`response.candidates[0].content.parts`, each a `Part` with `.text` or
`.function_call`) and a different message-history shape (`types.Content`
with role "user"/"model", not "user"/"assistant"). Rather than rewrite
discovery_loop.py around a second provider's shape, this file is a full
adapter: it does the Anthropic<->Gemini translation on both sides, so
`decide()` still takes and returns exactly what discovery_loop.py already
expects, and no other file has to change.

Uses `client.models.generate_content` (manual function-calling), not the
newer Interactions API, because it's the older, stably-documented surface
and easier to drive turn-by-turn ourselves. Model IDs and exact field names
on Google's side do shift over time — if a call fails outright, check
https://ai.google.dev/gemini-api/docs/function-calling against whatever
`google-genai` version actually gets installed.

One more Gemini-specific wrinkle this adapter has to carry: "thinking"
models (gemini-3.x) attach an opaque `thought_signature` (bytes) to the
Part that carries a function_call (sometimes to the final part generally),
and reject a follow-up request whose replayed history has a function_call
part missing the signature it originally issued for it — see
https://ai.google.dev/gemini-api/docs/thinking#signatures. Since this
adapter rebuilds Gemini `Content`/`Part` objects from the shim blocks on
every turn (discovery_loop.py just keeps appending to a plain `messages`
list, same as it did for Anthropic), each shim block below carries the
signature it was issued with, if any, purely so it can be echoed back
unchanged. It's opaque to us — we never inspect or generate it ourselves,
only round-trip whatever Gemini sent.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from google import genai
from google.genai import types

from agent.tools import TOOLS

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")


# --- Shim classes: mimic the Anthropic response-content-block shape that
# discovery_loop.py already reads, so that file needs zero changes. ---

class _TextBlock:
    type = "text"

    def __init__(self, text: str, thought_signature: bytes | None = None):
        self.text = text
        self.thought_signature = thought_signature


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict, thought_signature: bytes | None = None):
        self.id = id
        self.name = name
        self.input = input
        self.thought_signature = thought_signature


class _Response:
    def __init__(self, content: list):
        self.content = content


def _strip_unsupported_schema_keys(schema: Any) -> Any:
    """Anthropic's input_schema and Gemini's FunctionDeclaration.parameters
    are both roughly JSON Schema, but Gemini's schema validator is stricter
    about which keys it accepts. "default" (used by wait_for_text's
    timeout_ms) is the one key in agent/tools.py that isn't universally
    safe, so it's dropped here; the Python-side .get(..., 8000) fallback in
    discovery_loop.py still supplies the default when the model omits it."""
    if isinstance(schema, dict):
        return {k: _strip_unsupported_schema_keys(v) for k, v in schema.items() if k != "default"}
    if isinstance(schema, list):
        return [_strip_unsupported_schema_keys(v) for v in schema]
    return schema


class LLMClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.client = genai.Client(
            api_key=api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        )
        self.model = model or DEFAULT_MODEL
        self._function_declarations = [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": _strip_unsupported_schema_keys(t["input_schema"]),
            }
            for t in TOOLS
        ]
        # Gemini function calls don't carry a call-id the way Anthropic's
        # tool_use blocks do. We mint one per call (see _to_shim_response)
        # purely so discovery_loop.py's tool_use_id/tool_result correlation
        # still works, and remember which function name it belonged to so
        # the eventual tool_result can be turned back into a proper
        # FunctionResponse Part (see _to_gemini_contents).
        self._pending_call_names: dict[str, str] = {}

    def decide(self, system_prompt: str, messages: list[dict]):
        contents = self._to_gemini_contents(messages)
        response = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[types.Tool(function_declarations=self._function_declarations)],
                max_output_tokens=2048,
            ),
        )
        return self._to_shim_response(response)

    def _to_gemini_contents(self, messages: list[dict]) -> list:
        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            content = msg["content"]
            parts = []

            if isinstance(content, str):
                parts.append(types.Part(text=content))
            else:
                for block in content:
                    block_type = block["type"] if isinstance(block, dict) else getattr(block, "type", None)

                    if block_type == "text":
                        text = block["text"] if isinstance(block, dict) else block.text
                        sig = None if isinstance(block, dict) else getattr(block, "thought_signature", None)
                        parts.append(types.Part(text=text, thought_signature=sig))

                    elif block_type == "tool_use":
                        name = block["name"] if isinstance(block, dict) else block.name
                        args = block["input"] if isinstance(block, dict) else block.input
                        sig = None if isinstance(block, dict) else getattr(block, "thought_signature", None)
                        # Echo back the exact signature this function_call was issued with
                        # (see module docstring) — required by "thinking" models, harmless
                        # to set (as None) for models that don't use it.
                        parts.append(types.Part(
                            function_call=types.FunctionCall(name=name, args=args),
                            thought_signature=sig,
                        ))

                    elif block_type == "tool_result":
                        call_id = block["tool_use_id"]
                        result_text = block["content"]
                        name = self._pending_call_names.get(call_id, "unknown_tool")
                        parts.append(types.Part(function_response=types.FunctionResponse(
                            name=name, response={"result": result_text},
                        )))

            if parts:
                contents.append(types.Content(role=role, parts=parts))
        return contents

    def _to_shim_response(self, response) -> _Response:
        candidates = getattr(response, "candidates", None) or []
        if not candidates or not candidates[0].content or not candidates[0].content.parts:
            # Empty response (e.g. safety block, or the model produced
            # nothing this turn). Feed the loop a text block rather than
            # crashing — discovery_loop.py already re-prompts when a turn
            # has no tool_use, which is exactly the right recovery here.
            return _Response([_TextBlock("(model returned no content this turn)")])

        blocks: list = []
        for part in candidates[0].content.parts:
            sig = getattr(part, "thought_signature", None)

            text = getattr(part, "text", None)
            if text:
                blocks.append(_TextBlock(text, thought_signature=sig))
                continue

            fc = getattr(part, "function_call", None)
            if fc is not None:
                call_id = uuid.uuid4().hex
                args = dict(fc.args) if fc.args else {}
                self._pending_call_names[call_id] = fc.name
                blocks.append(_ToolUseBlock(call_id, fc.name, args, thought_signature=sig))

        if not blocks:
            blocks = [_TextBlock("(model returned no usable content this turn)")]
        return _Response(blocks)
