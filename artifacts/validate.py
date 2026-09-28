"""Shared input-param validation for a capability artifact.

Used by both replay/executor.py (the actual gate — checked before any
browser opens) and replay/run_replay.py's CLI pre-check, so the two can
never drift apart and disagree about what counts as valid input. A single
source of truth here is what lets ReplayOutcome.INPUT_ERROR mean the same
thing regardless of which caller triggered it.
"""
from __future__ import annotations

import re
from typing import Optional

from artifacts.schema import CapabilityArtifact


def validate_required_params(artifact: CapabilityArtifact, params: dict) -> Optional[str]:
    """Returns a human-readable error message if `params` doesn't satisfy
    `artifact.input_schema`, or None if it does."""
    for spec in artifact.input_schema:
        if spec.required and spec.name not in params:
            return f"Missing required input param '{spec.name}'."
        if spec.name in params and spec.validation_pattern:
            if not re.match(spec.validation_pattern, str(params[spec.name])):
                return (
                    f"Input param '{spec.name}' value does not match required "
                    f"pattern {spec.validation_pattern!r}."
                )
    return None
