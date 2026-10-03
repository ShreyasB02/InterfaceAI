"""
What an AI agent sees of this system: a catalog of capabilities it can call
by name with typed arguments, and one function to call them.

Everything upstream exists to make this small. An agent never sees steps,
locators, or a browser. It sees, per capability:

    name, description            what it does
    input_schema                 JSON Schema for the arguments
    output_schema                JSON Schema for what `success` returns
    outcomes                     the named non-success answers it can give
    requires_human_confirmation  whether a call may pause for a person

and gets back a `ReplayResult` whose `outcome` it can switch on.

`list_tools()` returns definitions in the shape function-calling APIs take
(`name` / `description` / `input_schema`), so they can be registered with a
model as tools directly. Only capabilities with a validly approved version
are listed: an unreviewed draft is not something an agent can discover, let
alone call. `invoke()` runs deterministic replay — no model — pinned to that
approved version.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from artifacts import repository
from artifacts.review import approval_is_valid
from artifacts.schema import CapabilityArtifact, ParamType, ReplayResult, RiskLevel
from replay.executor import ReplayExecutor

_JSON_TYPE = {ParamType.STRING: "string", ParamType.NUMBER: "number", ParamType.BOOLEAN: "boolean"}


class CapabilityNotFound(Exception):
    pass


def _approved(name: str) -> CapabilityArtifact:
    """The newest version of `name` whose approval still matches its content."""
    for version in reversed(repository.versions(name)):
        artifact = repository.load(name, version)
        if approval_is_valid(artifact):
            return artifact
    raise CapabilityNotFound(
        f"No approved capability named '{name}'. Call list_tools() to see what is available.")


def _input_schema(artifact: CapabilityArtifact) -> dict:
    properties: dict[str, dict] = {}
    for param in artifact.input_schema:
        prop: dict[str, Any] = {"type": _JSON_TYPE[param.type], "description": param.description}
        if param.validation_pattern:
            prop["pattern"] = param.validation_pattern
        if param.example:
            prop["examples"] = [param.example]
        properties[param.name] = prop
    return {"type": "object", "properties": properties,
            "required": [p.name for p in artifact.input_schema if p.required],
            "additionalProperties": False}


def _output_schema(artifact: CapabilityArtifact) -> dict:
    return {"type": "object",
            "properties": {o.name: {"type": _JSON_TYPE[o.type], "description": o.description}
                           for o in artifact.output_schema},
            "required": [o.name for o in artifact.output_schema]}


def tool_definition(artifact: CapabilityArtifact) -> dict:
    risky = [s for s in artifact.steps
             if s.requires_confirmation and s.risk_level in (RiskLevel.RISKY, RiskLevel.IRREVERSIBLE)]
    return {
        "name": artifact.name,
        "description": artifact.description,
        "input_schema": _input_schema(artifact),
        "output_schema": _output_schema(artifact),
        "outcomes": [{"code": o.code, "message": o.message}
                     for o in {o.code: o for o in artifact.known_outcomes}.values()],
        # True: a call pauses for a person at an irreversible step, unless
        # the caller is authorised to pass confirmed_by_review.
        "requires_human_confirmation": bool(risky),
        "version": artifact.version,
        "approved_by": artifact.review.reviewed_by if artifact.review else None,
    }


def list_tools() -> list[dict]:
    names = sorted({m.group("name") for p in repository.STORE_DIR.glob("*.json")
                    if (m := repository._FILENAME.match(p.name))})
    tools = []
    for name in names:
        try:
            tools.append(tool_definition(_approved(name)))
        except CapabilityNotFound:
            continue  # only drafts or rejected versions: not offered to an agent
    return tools


def invoke(name: str, arguments: dict, *, confirmed_by_review: bool = False,
           escalation_timeout_s: Optional[float] = 60, evidence_root: Path = Path("evidence/replay"),
           base_url: Optional[str] = None,
           on_escalation: Optional[Callable] = None) -> ReplayResult:
    """Call a capability. Always returns a ReplayResult; the caller switches
    on `result.outcome`:

      success           -> result.outputs, shaped per the tool's output_schema
      business_outcome  -> result.business_outcome.code: an answer, not an error
      input_error       -> the arguments don't satisfy input_schema; fix the call
      escalated         -> an irreversible step is waiting on a person
      failure / refused / interrupted -> see result.failure / result.interrupted

    `confirmed_by_review=True` lets an irreversible step run on the strength
    of the artifact's approval instead of pausing for a person. Without it,
    such a call waits up to `escalation_timeout_s` and then returns
    `escalated` rather than blocking the agent indefinitely.
    """
    artifact = _approved(name)
    executor = ReplayExecutor(artifact, Path(evidence_root))
    return executor.run(dict(arguments), base_url=base_url, auto_approve=confirmed_by_review,
                        escalation_timeout_s=escalation_timeout_s, on_escalation=on_escalation)
