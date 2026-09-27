"""
Shared value coercion for extracted text -> typed output values. Used by
both the discovery agent (when finalizing a run's outputs) and the replay
engine (when populating declared outputs), so a $-formatted balance
extracted from the page becomes a real number in both places, consistently.
"""
from __future__ import annotations

import re

from artifacts.schema import ParamType

_NUMERIC_STRIP = re.compile(r"[^0-9.\-]")


def coerce_output_value(raw: str, type_: ParamType):
    raw = raw.strip()
    if type_ == ParamType.NUMBER:
        cleaned = _NUMERIC_STRIP.sub("", raw)
        return float(cleaned) if cleaned not in ("", "-", ".") else None
    if type_ == ParamType.BOOLEAN:
        return raw.strip().lower() in ("true", "yes", "1")
    return raw
