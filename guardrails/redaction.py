"""
Redaction for anything written to logs or evidence. Applied defensively at
the log-writing boundary in both discovery and replay, not just at the
points we happen to remember are sensitive — the goal is that a secret
never makes it to disk even if some future code path forgets to think
about it.
"""
from __future__ import annotations

import re

# Value-shape patterns that should never appear in evidence regardless of
# which field they came from.
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(password|passcode|secret|token|api[_-]?key)\b\s*[:=]\s*\S+"
)

FIELD_NAME_DENYLIST = {"password", "passcode", "secret", "token", "api_key", "ssn", "credential"}


def redact_text(s: str) -> str:
    if not s:
        return s
    s = _SSN.sub("[REDACTED-SSN]", s)
    s = _CARD.sub("[REDACTED-CARD]", s)
    s = _KEY_VALUE_SECRET.sub(lambda m: f"{m.group(1)}=[REDACTED]", s)
    return s


def redact_field_value(field_name: str, field_type: str, value: str) -> str:
    name = (field_name or "").lower()
    if field_type == "password" or any(bad in name for bad in FIELD_NAME_DENYLIST):
        return "[REDACTED]"
    return redact_text(value)


def redact_dict(d: dict) -> dict:
    """Shallow redaction for a log event's tool_input / outputs dict."""
    out = {}
    for k, v in d.items():
        if isinstance(v, str):
            if any(bad in k.lower() for bad in FIELD_NAME_DENYLIST):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_text(v)
        else:
            out[k] = v
    return out
