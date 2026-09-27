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
        "name": "finish_stuck",
        "description": "Declare that you cannot safely or successfully complete the goal. Ends the run without an artifact.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]
