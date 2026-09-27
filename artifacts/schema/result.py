"""
The replay result contract — what the caller (an AI agent, in production)
gets back. The load-bearing design choice is keeping these three cases
structurally distinct rather than collapsing them into a boolean:

  SUCCESS            - the goal was achieved; `outputs` is populated per
                        the artifact's output_schema.
  BUSINESS_OUTCOME    - the flow completed and reached a *named, expected*
                        non-success result (e.g. "no such member",
                        "action not permitted"). This is data the caller
                        needs, not an error — conflating it with FAILURE
                        is called out as the most common mistake here.
  FAILURE             - replay could not complete: a locator never
                        resolved, a checkpoint never matched, a step timed
                        out after its recoverable retries were exhausted,
                        or execution left the allowlist. Carries enough
                        detail (which step, what was expected, what was
                        observed) to debug without re-running.

RECOVERABLE conditions (a known interstitial, a transient slow load) are
*not* a fourth outcome here — by definition they're handled during replay
and never reach the caller as such. What the caller does see, when a
recoverable path was taken, is `recovered_steps`: a transparency log of
what replay noticed and worked around, so a human reviewing a "successful"
run can still see it wasn't perfectly smooth.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ReplayOutcome(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"
    ESCALATED = "escalated"  # stopped and hand-off to a human is pending/occurred


class RecoveredStep(BaseModel):
    step_id: str
    condition: str = Field(description="What was detected, e.g. 'session_expired_interstitial'.")
    action_taken: str = Field(description="What replay did about it, e.g. 're-navigated and retried once'.")


class BusinessOutcome(BaseModel):
    code: str = Field(description="Stable machine-readable code, e.g. 'member_not_found'.")
    message: str


class FailureDetail(BaseModel):
    step_id: str
    expected: str
    observed: str
    message: str


class EscalationDetail(BaseModel):
    reason: str
    step_id: str
    intervention_request_id: str


class ReplayResult(BaseModel):
    outcome: ReplayOutcome
    artifact_id: str
    artifact_version: str
    run_id: str
    started_at: datetime
    finished_at: datetime

    outputs: dict[str, Any] = Field(default_factory=dict)
    business_outcome: Optional[BusinessOutcome] = None
    failure: Optional[FailureDetail] = None
    escalation: Optional[EscalationDetail] = None

    recovered_steps: list[RecoveredStep] = Field(default_factory=list)
    steps_executed: int = 0
    evidence_path: str = Field(description="Relative path under /evidence/ for this run's log.")

    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1000
