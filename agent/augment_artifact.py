"""
Deliberate second authoring pass over a discovery-produced artifact: adds
known_outcomes and recoverable_patterns based on the developer's own
knowledge of this app's edge cases (see agent/discovery_loop.py's module
docstring for why this is intentionally NOT something a single successful
discovery run could honestly claim to have discovered on its own).

Finds the right `after_step` by matching on the recorded step's button
text/role rather than a hardcoded step_id, so this still works if a real
discovery run took a slightly different number of steps than expected.

Usage:
    python -m agent.augment_artifact --capability-name lookup_member_balance
    python -m agent.augment_artifact --capability-name open_sub_account
"""
from __future__ import annotations

import argparse
from typing import Optional

from artifacts import repository
from artifacts.schema import (
    ActionType,
    CapabilityArtifact,
    LocatorMethod,
    LocatorSpec,
    LocatorStrategy,
    OutcomeMarker,
    RecoverablePattern,
    RecoveryAction,
)


def find_step_by_button_text(artifact: CapabilityArtifact, text: str) -> Optional[str]:
    text_lower = text.lower()
    for step in artifact.steps:
        if step.action != ActionType.CLICK or step.target is None:
            continue
        for strategy in step.target.strategies:
            if strategy.role_name and strategy.role_name.lower() == text_lower:
                return step.step_id
            if strategy.method == LocatorMethod.TEXT and strategy.value.lower() == text_lower:
                return step.step_id
    return None


def augment_lookup_member_balance(artifact: CapabilityArtifact) -> CapabilityArtifact:
    search_step = find_step_by_button_text(artifact, "Search")
    detail_step = find_step_by_button_text(artifact, "View Member")
    if not search_step or not detail_step:
        raise RuntimeError(
            f"Could not find expected steps to attach outcomes to (search={search_step}, "
            f"detail={detail_step}). This artifact's recorded flow doesn't match what this "
            "augmentation assumes — inspect it before hand-adjusting."
        )

    artifact.known_outcomes = artifact.known_outcomes + [
        OutcomeMarker(
            code="member_not_found", message="No member exists with the given ID.",
            after_step=search_step,
            detection=LocatorSpec(strategies=[LocatorStrategy(
                method=LocatorMethod.TEXT, value="No member found matching ID",
                reasoning="Exact banner text the app renders on a failed search.",
            )]),
        )
    ]
    artifact.recoverable_patterns = artifact.recoverable_patterns + [
        RecoverablePattern(
            condition="One-time session-expired interstitial on member detail load.",
            after_step=detail_step,
            detection=LocatorSpec(strategies=[LocatorStrategy(
                method=LocatorMethod.TEXT, value="Your session has expired",
                reasoning="Exact interstitial banner text.",
            )]),
            recovery_action=RecoveryAction.RELOAD_AND_RETRY, max_attempts=1,
        )
    ]
    return artifact


def augment_open_sub_account(artifact: CapabilityArtifact) -> CapabilityArtifact:
    continue_step = find_step_by_button_text(artifact, "Continue")
    detail_step = find_step_by_button_text(artifact, "View Member")
    if not continue_step:
        raise RuntimeError(f"Could not find the 'Continue' step to attach outcomes to.")

    artifact.known_outcomes = artifact.known_outcomes + [
        OutcomeMarker(
            code="permission_denied",
            message="Sub-account creation is blocked for this member (compliance hold).",
            after_step=continue_step,
            detection=LocatorSpec(strategies=[LocatorStrategy(
                method=LocatorMethod.TEXT, value="Action not permitted",
                reasoning="Exact banner text for a restricted member.",
            )]),
        ),
        OutcomeMarker(
            code="invalid_deposit_amount", message="The initial deposit did not meet the app's minimum.",
            after_step=continue_step,
            detection=LocatorSpec(strategies=[LocatorStrategy(
                method=LocatorMethod.TEXT, value="Initial deposit must be at least",
                reasoning="Exact validation banner text.",
            )]),
        ),
    ]
    if detail_step:
        artifact.recoverable_patterns = artifact.recoverable_patterns + [
            RecoverablePattern(
                condition="One-time session-expired interstitial on member detail load.",
                after_step=detail_step,
                detection=LocatorSpec(strategies=[LocatorStrategy(
                    method=LocatorMethod.TEXT, value="Your session has expired",
                    reasoning="Exact interstitial banner text.",
                )]),
                recovery_action=RecoveryAction.RELOAD_AND_RETRY, max_attempts=1,
            )
        ]
    return artifact


AUGMENTATIONS = {
    "lookup_member_balance": augment_lookup_member_balance,
    "open_sub_account": augment_open_sub_account,
}


def _bump_minor(version: str) -> str:
    major, minor, patch = (version.split(".") + ["0", "0"])[:3]
    return f"{major}.{int(minor) + 1}.0"


def main():
    parser = argparse.ArgumentParser(description="Add known_outcomes/recoverable_patterns to a discovered artifact.")
    parser.add_argument("--capability-name", required=True)
    parser.add_argument("--version", default=None)
    args = parser.parse_args()

    if args.capability_name not in AUGMENTATIONS:
        raise SystemExit(f"No augmentation defined for '{args.capability_name}'. "
                          f"Known: {list(AUGMENTATIONS)}")

    artifact = repository.load(args.capability_name, args.version)
    before = (len(artifact.known_outcomes), len(artifact.recoverable_patterns))
    artifact = AUGMENTATIONS[args.capability_name](artifact)
    artifact.version = _bump_minor(artifact.version)

    path = repository.save(artifact)
    after = (len(artifact.known_outcomes), len(artifact.recoverable_patterns))
    print(f"Augmented {args.capability_name} -> v{artifact.version}")
    print(f"known_outcomes: {before[0]} -> {after[0]}, recoverable_patterns: {before[1]} -> {after[1]}")
    print(f"Saved to {path}")


if __name__ == "__main__":
    main()
