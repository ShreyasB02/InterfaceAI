"""
The replay result contract — what the caller (an AI agent, in production)
gets back. The load-bearing design choice is keeping these cases
structurally distinct rather than collapsing them into a boolean:

  INPUT_ERROR         - the CALLER's params don't satisfy the artifact's
                        input_schema (missing a required field, or a value
                        that fails its validation_pattern). Returned before
                        the browser ever opens — see artifacts/validate.py.
                        Deliberately separate from FAILURE: this is a
                        caller mistake, not something the automation did
                        wrong, and a production caller should react to it
                        differently (fix the call, don't just retry it).
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
                        execution left the allowlist, or a pending
                        escalation was explicitly cancelled by an operator
                        (see EscalationAbandoned in escalation/transport.py
                        — a cancel is a deliberate "stop waiting", reported
                        the same way any other unrecoverable stop is).
                        Carries enough detail (which step, what was
                        expected, what was observed) to debug without
                        re-running.
  INTERRUPTED          - an operator ended the run outright (Tier 2 #7's
                        "interrupt", as opposed to "cancel" above, which
                        only unblocks a stuck escalation). Deliberately
                        distinct from FAILURE: nothing went wrong with the
                        automation itself, a human simply chose to stop it,
                        and a caller should treat that the way it would
                        treat any other deliberate abort — not retry it
                        automatically the way it might a transient
                        FAILURE. See `interrupted` below for detail.

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
    INPUT_ERROR = "input_error"  # caller's params invalid; browser never opened
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"
    ESCALATED = "escalated"  # stopped and hand-off to a human is pending/occurred
    INTERRUPTED = "interrupted"  # an operator ended the run outright (Tier 2 #7)


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
    screenshot: Optional[str] = Field(
        default=None, description="Path, relative to evidence_path, of the page at the moment of failure."
    )
    dom_snapshot: Optional[str] = Field(
        default=None, description="Path, relative to evidence_path, of the redacted page HTML at failure."
    )


class LocatorFallback(BaseModel):
    """A step whose primary (index-0) locator strategy did not resolve and a
    lower-ranked one was used instead. The run still succeeded — this is the
    drift signal: a tenant that starts reporting these is degrading before
    it becomes an outright failure."""
    step_id: str
    strategy_index: int = Field(description="Index of the strategy that resolved (always > 0 here).")
    method: str


class EscalationDetail(BaseModel):
    reason: str
    step_id: str
    intervention_request_id: str


class InterruptDetail(BaseModel):
    step_id: str = Field(description="Step in progress (or about to run) when the interrupt landed.")
    reason: str


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
    interrupted: Optional[InterruptDetail] = None

    recovered_steps: list[RecoveredStep] = Field(default_factory=list)
    locator_fallbacks: list[LocatorFallback] = Field(default_factory=list)
    steps_executed: int = 0
    evidence_path: str = Field(description="Relative path under /evidence/ for this run's log.")

    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1000
