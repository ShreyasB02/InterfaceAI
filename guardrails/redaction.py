"""
Redaction for anything written to logs or evidence. Applied defensively at
the log-writing boundary in both discovery and replay, not just at the
points we happen to remember are sensitive — the goal is that a secret
never makes it to disk even if some future code path forgets to think
about it.

Deliberately NOT applied to a capability's actual returned outputs
(ReplayResult.outputs, or the CLI's final JSON print of it) — an account
number or balance a capability was built to look up is the deliverable,
not incidental log content, and redacting it there would just break the
thing the capability exists to do. This mirrors the assignment's own open
use of plain IDs in its examples; the line drawn here is log/evidence vs.
the capability's own declared output_schema, not "sensitive vs. not."
"""
from __future__ import annotations

import re

# Value-shape patterns that should never appear in evidence regardless of
# which field they came from.
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")
# This app's own structured ID shapes (checking/savings/sub-account numbers,
# e.g. "SAV-10001-01") and rendered dollar amounts. Kept as their own
# patterns, not folded into a generic "looks numeric" rule, so a bare
# member_id (e.g. "10001") — used openly throughout this app and its
# artifacts, same as the assignment's own examples — is never caught by
# accident.
_ACCOUNT_ID = re.compile(r"\b(?:CHK|SAV|SUB)-\d+-\d+\b")
_DOLLAR_AMOUNT = re.compile(r"\$\d[\d,]*\.\d{2}\b")
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(password|passcode|secret|token|api[_-]?key)\b\s*[:=]\s*\S+"
)

FIELD_NAME_DENYLIST = {"password", "passcode", "secret", "token", "api_key", "ssn", "credential"}


def redact_text(s: str) -> str:
    if not s:
        return s
    s = _SSN.sub("[REDACTED-SSN]", s)
    s = _CARD.sub("[REDACTED-CARD]", s)
    s = _ACCOUNT_ID.sub("[REDACTED-ACCOUNT]", s)
    s = _DOLLAR_AMOUNT.sub("[REDACTED-AMOUNT]", s)
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
