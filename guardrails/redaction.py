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


def redact_value(value, key: str = ""):
    """Redact any JSON-shaped value, recursing into dicts and lists. A string
    under a denylisted key is replaced whole; every other string is
    pattern-redacted."""
    if isinstance(value, str):
        if key and any(bad in key.lower() for bad in FIELD_NAME_DENYLIST):
            return "[REDACTED]"
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, key) for v in value]
    return value


def redact_dict(d: dict) -> dict:
    """Redact a whole log event (or any dict), at every depth."""
    return redact_value(d)


def scrub_known(text: str, known: dict[str, str]) -> str:
    """Replace literal values this run knows to be record data — e.g. a
    member's name it just read off the page — with their label. Patterns
    can't recognise a name; knowing where it came from can. Longest first,
    so a value that contains another is replaced whole."""
    if not text:
        return text
    for value in sorted(known, key=len, reverse=True):
        if len(value) >= 3:
            text = text.replace(value, known[value])
    return text


def scrub_known_value(value, known: dict[str, str]):
    """scrub_known over every string in a JSON-shaped value (keys untouched)."""
    if isinstance(value, str):
        return scrub_known(value, known)
    if isinstance(value, dict):
        return {k: scrub_known_value(v, known) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_known_value(v, known) for v in value]
    return value


def generalize_for_artifact(text: str, params: dict, extracted: dict) -> str:
    """Make model-written text safe and reusable before it goes into an
    artifact: the description and checkpoint a model writes after one run
    naturally quote that run's record ("Alice Rivera", "$8150.32"). An
    artifact describes the capability, not the record it was recorded on.

    Order matters: value-shape redaction first (so an account number that
    embeds a param value is caught whole), then the run's extracted outputs
    and input params become {placeholders}."""
    if not text:
        return text
    text = redact_text(text)
    text = scrub_known(text, {str(v): "{" + k + "}" for k, v in extracted.items() if v})
    return scrub_known(text, {str(v): "{" + k + "}" for k, v in params.items() if v})
