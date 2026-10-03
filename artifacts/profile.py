"""
Vendor outcome profiles: the runtime conditions a vendor app is known to
produce, declared once per app as data, and applied to every artifact
recorded on that app.

Why this exists. A successful discovery run never sees "no such member" or
a session-expired interstitial, so those can't come from the recording.
They are properties of the *application*, not of one capability: the same
"no match" banner follows the Search button in every flow that searches. So
they are authored once, per vendor product (artifacts/profiles/*.json), and
each rule is anchored to a control rather than to a step number:

    "after":  {"click": "Search"}            # the control this state follows
    "detect": {"text": "No member found"}    # how to recognise the state

Applying a profile walks the artifact's steps, and wherever the flow clicks
an anchored control, attaches the rule to that step as a `known_outcome` or
`recoverable_pattern`. A rule whose anchor isn't in the flow is skipped: a
read-only lookup never reaches "Continue", so it doesn't get the deposit
validation outcome. A new capability on the same app gets its error
handling without anyone writing code for it.

The result is a new minor version of the artifact, a draft: its content
changed, so it needs review like any other (artifacts/review.py).

Multi-tenant note: the profile is keyed by vendor product, the same axis
artifacts are reused on. A tenant that words a banner differently would get
a small tenant-level profile layered over this one (not built).

CLI:
    python -m artifacts.profile apply lookup_member_balance
    python -m artifacts.profile show  "ACME Core Servicer Terminal"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from artifacts.schema import (
    ActionType,
    ArtifactStatus,
    CapabilityArtifact,
    LocatorMethod,
    LocatorSpec,
    LocatorStrategy,
    OutcomeMarker,
    RecoverablePattern,
    RecoveryAction,
)

PROFILES_DIR = Path(__file__).parent / "profiles"


class Anchor(BaseModel):
    click: str = Field(description="Accessible name of the control whose click this state can follow.")


class Detect(BaseModel):
    text: str = Field(description="Visible text that identifies the state.")


class OutcomeRule(BaseModel):
    code: str
    message: str
    after: Anchor
    detect: Detect
    reasoning: str = ""


class RecoveryRule(BaseModel):
    condition: str
    after: Anchor
    detect: Detect
    reasoning: str = ""
    recovery_action: RecoveryAction
    max_attempts: int = 1


class VendorProfile(BaseModel):
    vendor_product: str
    description: str = ""
    known_outcomes: list[OutcomeRule] = Field(default_factory=list)
    recoverable_patterns: list[RecoveryRule] = Field(default_factory=list)


class ProfileNotFound(Exception):
    pass


def _slug(vendor_product: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", vendor_product.lower()).strip("_")


def load_profile(vendor_product: Optional[str]) -> VendorProfile:
    if not vendor_product:
        raise ProfileNotFound("The artifact does not name a vendor product, so no profile applies.")
    path = PROFILES_DIR / f"{_slug(vendor_product)}.json"
    if not path.exists():
        raise ProfileNotFound(f"No outcome profile for vendor product '{vendor_product}' (looked for {path.name}).")
    return VendorProfile.model_validate_json(path.read_text())


def _steps_clicking(artifact: CapabilityArtifact, control_name: str) -> list[str]:
    """step_ids of every click on a control with this accessible name."""
    wanted = control_name.lower()
    hits = []
    for step in artifact.steps:
        if step.action != ActionType.CLICK or step.target is None:
            continue
        for strategy in step.target.strategies:
            if (strategy.role_name or "").lower() == wanted or (
                    strategy.method == LocatorMethod.TEXT and strategy.value.lower() == wanted):
                hits.append(step.step_id)
                break
    return hits


def _detection(detect: Detect, reasoning: str) -> LocatorSpec:
    return LocatorSpec(strategies=[LocatorStrategy(
        method=LocatorMethod.TEXT, value=detect.text,
        reasoning=reasoning or "Declared in the vendor outcome profile.")])


def apply_profile(artifact: CapabilityArtifact, profile: VendorProfile) -> tuple[CapabilityArtifact, list[str]]:
    """Attach every rule whose anchor appears in the flow. Idempotent: a rule
    already present on a step is not added again. Returns the artifact and a
    human-readable list of what was attached."""
    attached: list[str] = []
    have_outcomes = {(o.code, o.after_step) for o in artifact.known_outcomes}
    have_patterns = {(p.condition, p.after_step) for p in artifact.recoverable_patterns}

    for rule in profile.known_outcomes:
        for step_id in _steps_clicking(artifact, rule.after.click):
            if (rule.code, step_id) in have_outcomes:
                continue
            artifact.known_outcomes.append(OutcomeMarker(
                code=rule.code, message=rule.message, after_step=step_id,
                detection=_detection(rule.detect, rule.reasoning)))
            attached.append(f"outcome {rule.code} after {step_id} ('{rule.after.click}')")

    for rule in profile.recoverable_patterns:
        for step_id in _steps_clicking(artifact, rule.after.click):
            if (rule.condition, step_id) in have_patterns:
                continue
            artifact.recoverable_patterns.append(RecoverablePattern(
                condition=rule.condition, after_step=step_id,
                detection=_detection(rule.detect, rule.reasoning),
                recovery_action=rule.recovery_action, max_attempts=rule.max_attempts))
            attached.append(f"recovery '{rule.condition}' after {step_id} ('{rule.after.click}')")
    return artifact, attached


def _bump_minor(version: str) -> str:
    major, minor, _ = version.split(".")
    return f"{major}.{int(minor) + 1}.0"


def main(argv: Optional[list[str]] = None):
    from artifacts import repository

    parser = argparse.ArgumentParser(description="Apply a vendor outcome profile to a recorded artifact.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_apply = sub.add_parser("apply", help="Attach the vendor's known outcomes and recoveries; saves the next minor version as a draft.")
    p_apply.add_argument("name")
    p_apply.add_argument("--version", default=None)
    p_show = sub.add_parser("show", help="Print a vendor product's profile.")
    p_show.add_argument("vendor_product")
    args = parser.parse_args(argv)

    try:
        if args.command == "show":
            print(json.dumps(load_profile(args.vendor_product).model_dump(mode="json"), indent=2))
            return
        artifact = repository.load(args.name, args.version)
        profile = load_profile(artifact.target.vendor_product)
    except (ProfileNotFound, FileNotFoundError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    source_version = artifact.version
    artifact, attached = apply_profile(artifact, profile)
    if not attached:
        print(f"{args.name} v{source_version}: nothing to attach — no rule in the "
              f"'{profile.vendor_product}' profile is anchored to a control this flow uses "
              "(or they are already present).")
        return

    # New content is a new version, and nobody has reviewed it yet. The
    # version it was derived from stays in the store untouched.
    artifact.version = _bump_minor(source_version)
    artifact.status = ArtifactStatus.DRAFT
    artifact.review = None
    path = repository.save(artifact)
    print(f"Applied the '{profile.vendor_product}' profile to {args.name} v{source_version} -> v{artifact.version} (draft)")
    for line in attached:
        print(f"  + {line}")
    print(f"Saved to {path}")


if __name__ == "__main__":
    main()
