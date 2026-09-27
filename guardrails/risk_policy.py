"""
The safety guardrail policy for replay: what happens when execution
reaches a step the artifact has flagged as needing care.

Kept as one small, readable function rather than scattered checks, because
this is the file a reviewer should be able to read to understand the whole
policy: a step whose artifact-declared risk is RISKY or IRREVERSIBLE and
which requires_confirmation is never auto-executed. It is escalated to a
human, full stop — the only override is an explicit, logged
--auto-approve flag on the replay CLI, meant for repeatable testing/demo
runs, never for unattended production replay of an unreviewed artifact.
"""
from __future__ import annotations

from artifacts.schema import RiskLevel, Step


def needs_escalation(step: Step, auto_approve: bool) -> bool:
    if not step.requires_confirmation:
        return False
    if step.risk_level not in (RiskLevel.RISKY, RiskLevel.IRREVERSIBLE):
        return False
    return not auto_approve


def is_unattended_safe(artifact_status: str) -> bool:
    """A draft artifact (the default status a discovery run produces) should
    not be trusted for unattended production replay — only one explicitly
    marked 'approved' (see ArtifactStatus) should be. Replay still allows
    running a draft artifact interactively (that's how you'd validate and
    approve it), it just won't claim the run was unattended-safe."""
    return artifact_status == "approved"
