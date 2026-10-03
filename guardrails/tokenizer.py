"""
Two-way tokenization for LLM-facing text (Tier 2 #6) — deliberately a
separate mechanism from guardrails/redaction.py's one-way log/evidence
redaction, because the two solve different problems:

  - redaction.py: irreversible. A value matching a sensitive shape is
    replaced with a fixed label ("[REDACTED-SSN]") when writing to
    logs/evidence. Nothing downstream ever needs the real value back out
    of a log line, so there's no reason to keep one.
  - tokenizer.py (this file): reversible, per-run. A value matching the
    same sensitive shapes is replaced with a stable placeholder
    ("[[TOK1]]") *before that text is ever placed into the LLM's own
    context* — the observation text shown each turn, the system prompt's
    param values, and any page-derived text echoed back as a tool result.
    The model reasons about "[[TOK1]]" and can even copy it verbatim into
    a later tool call (e.g. filling a field with a value it saw earlier)
    without the real value ever entering its context window or being sent
    to the LLM provider. Immediately before a token would drive a real
    browser action (a `fill`, a `wait_for_text`) it is detokenized back to
    the real value at that single boundary, in discovery_loop.py's tool
    dispatch — never anywhere in the LLM-facing path.

Reuses the exact same value-shape patterns redaction.py already maintains
(SSN, card, this app's structured account IDs, dollar amounts) rather than
keeping a second copy that could silently drift out of sync with it.
`_KEY_VALUE_SECRET` is deliberately NOT included here: it's a
structural "key=value" log pattern (password=..., token=...), not a
value-shape a legitimate field would ever legitimately need to fill or
compare against, so it stays redaction-only.

Token maps are held purely in memory, one Tokenizer per discovery run, and
are never written to disk — a token like "[[TOK3]]" leaking into a log
line is meaningless on its own, which is the entire point.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from guardrails.redaction import _ACCOUNT_ID, _CARD, _DOLLAR_AMOUNT, _SSN

# Order matters only in that longer/more specific shapes (SSN, card,
# account id) are tried before the loosest one (dollar amount); patterns
# don't overlap in practice so this is mostly documentation.
VALUE_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("SSN", _SSN),
    ("CARD", _CARD),
    ("ACCOUNT", _ACCOUNT_ID),
    ("AMOUNT", _DOLLAR_AMOUNT),
]

TOKEN_RE = re.compile(r"\[\[TOK\d+\]\]")

TOKEN_EXPLAINER = (
    "Some values below — input values, or values read from the page — may appear as opaque "
    "placeholders like [[TOK1]] instead of their real contents. This happens for values whose "
    "shape looks sensitive (account numbers, card numbers, SSNs, dollar amounts): you are shown "
    "a stable placeholder instead of the real value so it never has to pass through you. Treat "
    "each placeholder as if it were that value — copy it verbatim into a tool call that needs it "
    "(e.g. fill(index=N, value=\"[[TOK1]]\")). The same real value always maps to the same "
    "placeholder within this run, so two placeholders that match mean the underlying values "
    "match too. You do not need to know, and cannot know, what a placeholder actually contains."
)


@dataclass
class Tokenizer:
    """One instance per discovery run. Not thread-safe — a discovery run is
    single-threaded, and state here is purely in-memory scratch, discarded
    with the run object itself."""

    _value_to_token: dict[str, str] = field(default_factory=dict)
    _token_to_value: dict[str, str] = field(default_factory=dict)
    _next_id: int = 1

    def _mint(self, value: str) -> str:
        existing = self._value_to_token.get(value)
        if existing:
            return existing
        token = f"[[TOK{self._next_id}]]"
        self._next_id += 1
        self._value_to_token[value] = token
        self._token_to_value[token] = value
        return token

    def tokenize(self, text: Optional[str]) -> Optional[str]:
        """Replace every value-shape match in `text` with a stable
        placeholder, minting a new one the first time a given value is
        seen and reusing it on every later occurrence (including across
        turns), so the model can recognize "the same value as before"
        without ever seeing what it is.

        After the shape-pattern pass, also does a literal pass over every
        value already known to this tokenizer (including ones opted in via
        register_known_value() that don't match any VALUE_PATTERNS shape on
        their own) — so a value a caller has explicitly flagged as
        sensitive gets substituted wherever it later shows up in
        LLM-facing text, not just at the call site that registered it.
        Checked longest-value-first so one registered value can't clobber
        a substring of a longer one."""
        if not text:
            return text
        out = text
        for _, pattern in VALUE_PATTERNS:
            out = pattern.sub(lambda m: self._mint(m.group(0)), out)
        if self._value_to_token:
            for value, tok in sorted(self._value_to_token.items(), key=lambda kv: -len(kv[0])):
                if value and value in out:
                    out = out.replace(value, tok)
        return out

    def detokenize(self, text: Optional[str]) -> Optional[str]:
        """Reverse of tokenize(): swap every [[TOKn]] placeholder back to
        its real value. Called at the single boundary where a value is
        about to drive a real browser action — never on the LLM-facing
        side of that boundary. A placeholder with no known mapping (should
        not happen, but a model could in principle hallucinate one) is
        left as-is rather than raising, so a hallucinated token surfaces as
        a harmless literal string typed into a field rather than crashing
        the run."""
        if not text:
            return text
        return TOKEN_RE.sub(lambda m: self._token_to_value.get(m.group(0), m.group(0)), text)

    def register_known_value(self, value: Optional[str]) -> Optional[str]:
        """Pre-mint (or reuse) a token for a value the harness already
        knows should never reach the model as-is, even if its shape
        doesn't match one of VALUE_PATTERNS (e.g. a caller-supplied param
        the harness has been told is sensitive). Returns the value
        unchanged if it's falsy."""
        if not value:
            return value
        return self._mint(value)
