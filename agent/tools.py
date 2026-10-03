"""
The tool surface offered to the LLM during discovery. Kept deliberately
small and generic (navigate/click/fill/wait/extract/finish) — the model
reasons about *this* app's specific fields and buttons purely from what
observe() shows it each turn, the same way a human operator would with no
prior knowledge of the page.
"""

TOOLS = [
    {
        "name": "navigate",
        "description": "Navigate the browser to a path or URL within the target app.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path or full URL to navigate to, e.g. '/members/search'."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "click",
        "description": (
            "Click an interactive element by its index from the last observation. "
            "If this click might trigger a native browser confirmation dialog "
            "(e.g. a button whose label suggests an irreversible action), set "
            "on_dialog to 'accept' or 'dismiss' to say what should happen to it. "
            "If you don't expect a dialog, omit on_dialog."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Element index from the last observation."},
                "on_dialog": {"type": "string", "enum": ["accept", "dismiss"]},
            },
            "required": ["index"],
        },
    },
    {
        "name": "fill",
        "description": "Type a value into a text/password input field by its index from the last observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer"},
                "value": {"type": "string"},
            },
            "required": ["index", "value"],
        },
    },
    {
        "name": "wait_for_text",
        "description": "Wait until specific text appears on the page. Use this for slow-loading pages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 8000},
            },
            "required": ["text"],
        },
    },
    {
        "name": "extract_field",
        "description": (
            "Read a labeled value off the current page (e.g. a table row whose "
            "first cell reads 'Savings') and record it as one of this capability's "
            "declared outputs. cell_index is 0-based within the matched row; use -1 "
            "(default) for the last cell, which is usually the value cell."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Row label text to match, e.g. 'Savings' or 'Name'."},
                "output_name": {"type": "string", "description": "Which declared output field this fills."},
                "cell_index": {"type": "integer", "default": -1},
            },
            "required": ["label", "output_name"],
        },
    },
    {
        "name": "finish_success",
        "description": "Declare the goal achieved. Ends the run and builds the reusable artifact from the recorded steps.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One sentence: what was accomplished."},
                "checkpoint_description": {
                    "type": "string",
                    "description": "What on the final page proves the goal was reached, e.g. 'success banner with new account number is visible'.",
                },
            },
            "required": ["summary", "checkpoint_description"],
        },
    },
    {
        "name": "request_human",
        "description": (
            "Hand this same live session to a human operator when you cannot safely proceed but a "
            "person could: a decision you are not authorized to make, a control you cannot find, an "
            "unexpected screen. The run pauses, the operator acts on the page, then control returns "
            "to you with a description of what they did and the new page state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why you are stopping and what you need the human to do."},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "finish_stuck",
        "description": (
            "Declare a dead end that a human operator could not fix either (e.g. the page reports the "
            "record does not exist). Ends the run without an artifact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]

# Every tool takes a required `reason`: why this action, given what the model
# sees. Some providers return a tool call with no accompanying text, which
# would leave the run's log saying what was done but not why; making the
# rationale an argument gets it on every call from every provider. The
# harness logs it and strips it before acting — it never changes what the
# tool does. request_human and finish_stuck already define their own
# `reason`, which serves the same purpose.
_REASON = {
    "type": "string",
    "description": "One sentence: why you are taking this action now, given what the page shows.",
}
for _tool in TOOLS:
    _schema = _tool["input_schema"]
    if "reason" not in _schema["properties"]:
        _schema["properties"]["reason"] = _REASON
        _schema["required"] = [*_schema.get("required", []), "reason"]
