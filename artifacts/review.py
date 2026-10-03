"""
Human review of a capability artifact: the draft -> approved | rejected gate.

A discovery run only ever produces a `draft`. Nothing replays a draft
unattended (guardrails/risk_policy.py) — a person has to read the artifact
and approve it first. Approval is bound to what was actually reviewed:
`content_hash()` covers everything that determines what replay will DO
(steps, locators, I/O contract, checkpoint, outcome and recovery rules), and
is stored on the review record. If any of that is edited afterwards the hash
no longer matches and the approval stops counting.

CLI:
    python -m artifacts.review list
    python -m artifacts.review show    lookup_member_balance [--version 1.1.0]
    python -m artifacts.review approve lookup_member_balance --reviewer alice [--notes "..."]
    python -m artifacts.review reject  lookup_member_balance --reviewer alice --notes "..."
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Optional

from artifacts.schema import ArtifactStatus, CapabilityArtifact, ReviewRecord

# What replay executes. Deliberately excludes status/review (they'd make the
# hash depend on itself), provenance and timestamps (they don't change
# behaviour), and target.base_url (the same approved flow is dispatched
# against different tenants' hosts).
_REVIEWED_FIELDS = {"name", "version", "input_schema", "output_schema", "steps", "checkpoint",
                    "known_outcomes", "recoverable_patterns"}


def content_hash(artifact: CapabilityArtifact) -> str:
    payload = artifact.model_dump(mode="json", include=_REVIEWED_FIELDS)
    payload["entry_path"] = artifact.target.entry_path
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def approval_is_valid(artifact: CapabilityArtifact) -> bool:
    """True only for an approved artifact whose content is still exactly
    what the reviewer approved."""
    return (artifact.status == ArtifactStatus.APPROVED and artifact.review is not None
            and artifact.review.content_hash == content_hash(artifact))


def _record(artifact: CapabilityArtifact, status: ArtifactStatus, reviewer: str,
            notes: Optional[str]) -> CapabilityArtifact:
    artifact.status = status
    artifact.review = ReviewRecord(reviewed_by=reviewer, reviewed_at=datetime.now(timezone.utc),
                                   notes=notes, content_hash=content_hash(artifact))
    return artifact


def approve(artifact: CapabilityArtifact, reviewer: str, notes: Optional[str] = None) -> CapabilityArtifact:
    return _record(artifact, ArtifactStatus.APPROVED, reviewer, notes)


def reject(artifact: CapabilityArtifact, reviewer: str, notes: Optional[str] = None) -> CapabilityArtifact:
    return _record(artifact, ArtifactStatus.REJECTED, reviewer, notes)


def _summarize(artifact: CapabilityArtifact) -> str:
    """The reviewer's view: what it does, needs, returns, and where the risk is."""
    lines = [
        f"{artifact.name} v{artifact.version}  [{artifact.status.value}]",
        f"  {artifact.description}",
        f"  recorded by {artifact.provenance.model_provider}/{artifact.provenance.model_name} "
        f"in run {artifact.provenance.discovery_run_id}"
        + (f" ({artifact.provenance.human_interventions} human intervention(s))"
           if artifact.provenance.human_interventions else ""),
        "  inputs:  " + (", ".join(f"{p.name}:{p.type.value}" for p in artifact.input_schema) or "(none)"),
        "  outputs: " + (", ".join(f"{o.name}:{o.type.value}" for o in artifact.output_schema) or "(none)"),
        "  steps:",
    ]
    for step in artifact.steps:
        flags = []
        if step.origin == "human":
            flags.append("recorded from a human operator")
        if step.requires_confirmation:
            flags.append(f"{step.risk_level.value}, handed to a human at replay")
        flag = f"  <-- {'; '.join(flags)}" if flags else ""
        lines.append(f"    {step.step_id:>4} {step.action.value:<13} {step.intent}{flag}")
    lines.append(f"  checkpoint: {artifact.checkpoint.method.value} {artifact.checkpoint.value!r}")
    lines.append("  known outcomes: " + (", ".join(o.code for o in artifact.known_outcomes) or "(none)"))
    lines.append("  recoverable:    " + (", ".join(p.condition for p in artifact.recoverable_patterns) or "(none)"))
    if artifact.review:
        valid = "" if artifact.status != ArtifactStatus.APPROVED or approval_is_valid(artifact) \
            else "  ** content changed since this review — approval no longer counts **"
        lines.append(f"  review: {artifact.status.value} by {artifact.review.reviewed_by} "
                     f"at {artifact.review.reviewed_at.isoformat()}{valid}")
    return "\n".join(lines)


def main():
    from artifacts import repository

    parser = argparse.ArgumentParser(description="Review capability artifacts (draft -> approved | rejected).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Every stored artifact version and its review state.")
    for command in ("show", "approve", "reject"):
        p = sub.add_parser(command)
        p.add_argument("name")
        p.add_argument("--version", default=None, help="Exact version or prefix; defaults to the latest.")
        if command != "show":
            p.add_argument("--reviewer", required=True, help="Who is accountable for this decision.")
            p.add_argument("--notes", default=None, required=(command == "reject"))
    args = parser.parse_args()

    if args.command == "list":
        for row in repository.list_artifacts():
            if "error" in row:
                print(f"{row['path']}: unreadable ({row['error']})")
            else:
                print(f"{row['name']:<28} v{row['version']:<8} {row['status']:<9} "
                      f"{row['reviewed_by'] or '-'}")
        return

    try:
        artifact = repository.load(args.name, args.version)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    if args.command == "show":
        print(_summarize(artifact))
        return

    decide = approve if args.command == "approve" else reject
    decide(artifact, args.reviewer, args.notes)
    repository.save(artifact, allow_overwrite=True)  # a review-state transition, not a content change
    print(_summarize(artifact))


if __name__ == "__main__":
    main()
