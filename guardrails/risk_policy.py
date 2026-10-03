"""
The safety guardrail policy for replay. Kept as two small functions in one
file, because this is the file a reviewer should be able to read to
understand the whole policy.

1. Which artifacts may run at all (`replay_refusal`), checked before a
   browser opens:

     approved, content unchanged since review  -> may run, attended or not
     approved, content changed since review    -> refused (the approval was
                                                   for different content)
     draft                                      -> only attended: a person is
                                                   running it to validate it,
                                                   which is how it gets approved
     rejected                                   -> refused

2. What happens at a risky step (`needs_escalation`): a step the artifact
   marks RISKY/IRREVERSIBLE with requires_confirmation is escalated to a
   human rather than executed. The only override is auto_approve, and
   `replay_refusal` only allows that on a validly approved artifact — there
   the human review that approved the artifact is the confirmation for its
   risky step. An unreviewed draft can never skip the human.
"""
from __future__ import annotations

from typing import Optional

from artifacts.review import approval_is_valid
from artifacts.schema import ArtifactStatus, CapabilityArtifact, RiskLevel, Step


def replay_refusal(artifact: CapabilityArtifact, attended: bool, auto_approve: bool) -> Optional[str]:
    """Why this artifact must not be replayed under these conditions, or
    None if it may."""
    label = f"{artifact.name} v{artifact.version}"
    if artifact.status == ArtifactStatus.REJECTED:
        return f"{label} was rejected in review and must not be replayed."
    if artifact.status == ArtifactStatus.APPROVED and not approval_is_valid(artifact):
        return (f"{label} is marked approved, but its content has changed since it was reviewed. "
                "Re-review it (python -m artifacts.review approve ...).")
    if artifact.status == ArtifactStatus.DRAFT:
        if auto_approve:
            return (f"{label} is an unreviewed draft: its irreversible steps cannot be auto-approved. "
                    "Approve the artifact first, or let the step escalate to a human.")
        if not attended:
            return (f"{label} is an unreviewed draft and cannot be replayed unattended. Approve it "
                    "(python -m artifacts.review approve ...), or run it attended to validate it.")
    return None


def needs_escalation(step: Step, auto_approve: bool) -> bool:
    if not step.requires_confirmation:
        return False
    if step.risk_level not in (RiskLevel.RISKY, RiskLevel.IRREVERSIBLE):
        return False
    return not auto_approve
