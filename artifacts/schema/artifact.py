"""
The capability artifact schema — the reusable, agent-invocable contract that
a successful discovery run produces and that the replay engine executes
without any LLM in the loop.

Design intent (see /REPORT.md "Artifact schema" for the full rationale):

  - Every step's target is a *ranked list* of locator strategies, not a
    single selector. Replay tries them in order and stops at the first
    strategy that resolves to exactly one element. This is the seam that
    lets the same artifact survive small DOM changes, and is also the seam
    a per-tenant override would slot into (add/replace strategies for one
    tenant without touching step semantics).
  - A step's `intent` is a human-readable description of *what* it
    accomplishes, kept separate from `action`/`target` (the *how*). A human
    reviewer — or a future re-recording pass — can read intent without
    parsing selectors.
  - Dynamic values reference input params by name (`value_param`) rather
    than being string-templated, so the typed input contract is the single
    source of truth for what an invocation needs.
  - Risk is tracked per-step, not just per-artifact, because a single
    capability (e.g. "open sub-account") is mostly safe/reversible read
    steps followed by exactly one irreversible write — collapsing that to
    one artifact-level flag would either over-block the reads or
    under-protect the write.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Locator strategy — how a step's target element is identified
# --------------------------------------------------------------------------

class LocatorMethod(str, Enum):
    ROLE = "role"          # accessibility role + accessible name (most robust)
    CSS = "css"            # CSS selector (e.g. input[name=...]) — stable when
                            # the app uses meaningful name/structural attrs
    TEXT = "text"          # exact/substring visible text match
    XPATH = "xpath"        # structural XPath, last resort for tables with no
                            # other stable hook (e.g. "cell next to this label")


class LocatorStrategy(BaseModel):
    method: LocatorMethod
    value: str
    role_name: Optional[str] = Field(
        default=None, description="Accessible name, only used when method == role"
    )
    reasoning: str = Field(
        description="Why this strategy was picked and how robust it's expected to be "
        "against runtime DOM variation (not layout drift — see REPORT.md)."
    )


class LocatorSpec(BaseModel):
    """An ordered fallback chain. Replay tries strategies in order and uses
    the first one that resolves to exactly one visible element."""
    strategies: list[LocatorStrategy]

    def primary(self) -> LocatorStrategy:
        return self.strategies[0]


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    WAIT_FOR = "wait_for"          # wait for a locator/text/url condition
    ASSERT_TEXT = "assert_text"    # mid-flow checkpoint, not the final one
    EXTRACT = "extract"            # read a value into the output set
    HANDLE_DIALOG = "handle_dialog"  # accept/dismiss a native browser dialog


class RiskLevel(str, Enum):
    SAFE = "safe"                  # read-only, fully reversible
    REVERSIBLE = "reversible"      # writes, but trivially undoable
    RISKY = "risky"                # writes with real-world effect
    IRREVERSIBLE = "irreversible"  # cannot be undone by the agent itself


class Step(BaseModel):
    step_id: str
    intent: str = Field(description="What this step accomplishes, in plain language.")
    action: ActionType
    target: Optional[LocatorSpec] = Field(
        default=None, description="Required for click/fill/wait_for/assert_text/extract."
    )
    value_literal: Optional[str] = Field(
        default=None, description="Static value to type/assert, if not param-derived."
    )
    value_param: Optional[str] = Field(
        default=None, description="Name of the input param supplying this step's value."
    )
    output_name: Optional[str] = Field(
        default=None, description="For action == extract: which output field this fills."
    )
    on_dialog: Optional[str] = Field(
        default=None, description="'accept' or 'dismiss', for action == handle_dialog."
    )
    timeout_ms: int = 5000
    risk_level: RiskLevel = RiskLevel.SAFE
    requires_confirmation: bool = Field(
        default=False,
        description="If true, replay must escalate to a human before executing this "
        "step rather than proceeding automatically, regardless of risk_level default policy.",
    )


# --------------------------------------------------------------------------
# Typed I/O
# --------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    example: Optional[str] = None
    validation_pattern: Optional[str] = Field(
        default=None, description="Optional regex the caller's value must match."
    )


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str


# --------------------------------------------------------------------------
# Checkpoint — the final success condition
# --------------------------------------------------------------------------

class CheckpointMethod(str, Enum):
    URL_MATCHES = "url_matches"
    TEXT_PRESENT = "text_present"
    ELEMENT_PRESENT = "element_present"


class Checkpoint(BaseModel):
    description: str
    method: CheckpointMethod
    value: str
    target: Optional[LocatorSpec] = Field(
        default=None, description="Required when method == element_present."
    )


# --------------------------------------------------------------------------
# Known outcomes & recoverable patterns — declared on the artifact so the
# three-way result contract (success / business outcome / hard failure) is
# something a reviewer can read off the artifact, not logic buried in the
# replay engine. Checked after a named step, before the next step runs.
# --------------------------------------------------------------------------

class OutcomeMarker(BaseModel):
    """A named, expected non-success result — not a failure, a legitimate
    answer the caller needs (e.g. 'no such member')."""
    code: str
    message: str
    after_step: str = Field(description="step_id after which this marker is checked.")
    detection: LocatorSpec


class RecoveryAction(str, Enum):
    RETRY_STEP = "retry_step"
    RELOAD_AND_RETRY = "reload_and_retry"
    DISMISS_AND_CONTINUE = "dismiss_and_continue"


class RecoverablePattern(BaseModel):
    """A runtime condition that legitimately occurs and should be handled
    deliberately rather than treated as a hard failure — e.g. a known
    interstitial or a transient slow load."""
    condition: str
    after_step: str = Field(description="step_id after which this pattern is checked.")
    detection: LocatorSpec
    recovery_action: RecoveryAction
    max_attempts: int = 1


# --------------------------------------------------------------------------
# Provenance — link back to the discovery run without embedding the raw
# model transcript in the artifact itself (kept decoupled per the brief).
# --------------------------------------------------------------------------

class DiscoveryProvenance(BaseModel):
    goal: str
    discovery_run_id: str
    model_provider: str
    model_name: str
    recorded_at: datetime
    evidence_path: str = Field(description="Relative path under /evidence/ for the raw run.")


class TargetSurface(BaseModel):
    surface_type: str = Field(description="'web' | 'legacy_web' | 'desktop' (see REPORT.md)")
    base_url: str
    entry_path: str = Field(description="Path artifact navigates to first, e.g. /members/search")
    vendor_product: Optional[str] = Field(
        default=None,
        description="Identifies the underlying vendor product/UI this artifact was recorded "
        "against, so it can be matched to other tenants running the same product.",
    )


class ArtifactStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


# --------------------------------------------------------------------------
# The artifact itself
# --------------------------------------------------------------------------

class CapabilityArtifact(BaseModel):
    artifact_id: str
    name: str = Field(description="Stable capability name an agent invokes by, e.g. "
                       "'lookup_member_balance'.")
    version: str = Field(description="Semver, e.g. '1.0.0'.")
    description: str = Field(description="What this capability does — for a human reviewer "
                              "and a calling agent deciding whether to invoke it.")
    status: ArtifactStatus = ArtifactStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    provenance: DiscoveryProvenance
    target: TargetSurface

    input_schema: list[InputParam]
    output_schema: list[OutputField]
    steps: list[Step]
    checkpoint: Checkpoint
    known_outcomes: list[OutcomeMarker] = Field(default_factory=list)
    recoverable_patterns: list[RecoverablePattern] = Field(default_factory=list)

    default_risk_level: RiskLevel = RiskLevel.SAFE

    class Config:
        use_enum_values = False
