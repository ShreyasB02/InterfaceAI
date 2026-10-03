"""Shared input validation for a capability artifact: the artifact's
`input_schema` is a contract, and this is where it is enforced.

Used by replay/executor.py (the actual gate, checked before any browser
opens) and by the replay CLI's pre-check, so the two can never disagree
about what counts as valid input. ReplayOutcome.INPUT_ERROR means the same
thing whichever caller triggered it.

Values may arrive typed (an agent's JSON arguments: 100, true) or as strings
(the CLI: "100"), so a number may be numeric or a numeric string.
"""
from __future__ import annotations

import re
from typing import Optional

from artifacts.schema import CapabilityArtifact, ParamType


def _type_error(value, type_: ParamType) -> Optional[str]:
    if type_ == ParamType.NUMBER:
        if isinstance(value, bool):
            return "a number, not a boolean"
        if isinstance(value, (int, float)):
            return None
        try:
            float(str(value))
            return None
        except ValueError:
            return "a number"
    if type_ == ParamType.BOOLEAN:
        if isinstance(value, bool) or str(value).lower() in ("true", "false"):
            return None
        return "true or false"
    if isinstance(value, (dict, list)) or value is None:
        return "a string"
    return None


def validate_required_params(artifact: CapabilityArtifact, params: dict) -> Optional[str]:
    """Returns a human-readable error message if `params` doesn't satisfy
    `artifact.input_schema`, or None if it does."""
    declared = {spec.name for spec in artifact.input_schema}
    unexpected = sorted(set(params) - declared)
    if unexpected:
        return (f"Unexpected input param(s) {unexpected}; this capability takes {sorted(declared)}.")
    for spec in artifact.input_schema:
        if spec.name not in params:
            if spec.required:
                return f"Missing required input param '{spec.name}'."
            continue
        value = params[spec.name]
        wanted = _type_error(value, spec.type)
        if wanted:
            return f"Input param '{spec.name}' must be {wanted}; got {value!r}."
        if spec.validation_pattern and not re.match(spec.validation_pattern, str(value)):
            return (f"Input param '{spec.name}' value does not match required "
                    f"pattern {spec.validation_pattern!r}.")
    return None
