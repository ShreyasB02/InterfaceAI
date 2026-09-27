"""
The discovery loop: observe -> decide -> act, driven by the LLM, against a
live browser session. A successful run's recorded actions become a
CapabilityArtifact; the raw model transcript stays in /evidence/ as
provenance, never embedded in the artifact itself.

Design choices worth flagging (expanded in /REPORT.md):

  - Session/auth is bootstrapped by the harness *before* the loop starts
    and is never part of the recorded steps. A capability artifact assumes
    an authenticated context, the same way a production API client
    attaches auth before calling any endpoint — this is also what keeps an
    artifact portable across tenants with different login flows.
  - The initial navigation to the capability's entry point is likewise
    performed by the harness, not "discovered" — where to start is given,
    not something worth spending a model call on.
  - A step is marked irreversible + requires_confirmation automatically
    when the click that produced it triggered a real native confirmation
    dialog — risk classification comes from what actually happened, not a
    guess made after the fact.
  - known_outcomes / recoverable_patterns are intentionally NOT populated
    from a single discovery run: a successful run, by definition, didn't
    hit them. They're added as a deliberate second authoring pass (see
    agent/augment_artifact.py) — the same way an engineer who owns this
    system would encode edge cases they've tested for, not something a
    single linear trace could honestly claim to have discovered.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from artifacts.schema import (
    ActionType,
    CapabilityArtifact,
    Checkpoint,
    CheckpointMethod,
    DiscoveryProvenance,
    InputParam,
    LocatorMethod,
    LocatorSpec,
    LocatorStrategy,
    OutputField,
    ParamType,
    RiskLevel,
    Step,
    TargetSurface,
)
from agent.browser_surface import ActionRecord, BrowserSurface
from agent.llm_client import LLMClient
from agent.locator_inference import derive_locator_strategies
from guardrails.allowlist import Allowlist, AllowlistViolation
from guardrails.redaction import redact_dict

EMPLOYEE_ID = "EMP001"
PASSCODE = "demo1234"

MAX_STEPS = 25
WALL_TIMEOUT_S = 180


class DiscoveryFailed(Exception):
    def __init__(self, reason: str, run_id: str):
        super().__init__(reason)
        self.reason = reason
        self.run_id = run_id


def _infer_param_type(name: str, value: str, declared: dict[str, str]) -> ParamType:
    if name in declared:
        return ParamType(declared[name])
    # Default to STRING, not a numeric guess: identifiers that merely look
    # numeric (member IDs, account numbers) would silently lose leading
    # zeros if we inferred NUMBER from "parses as float". Only a value the
    # caller explicitly typed as a number (via --param-type) is treated as
    # one; everything else is a string, which is the safer default for a
    # legacy app where most fields are opaque IDs.
    return ParamType.STRING


def _build_system_prompt(goal: str, entry_path: str, params: dict[str, str],
                          outputs: list[dict]) -> str:
    param_lines = "\n".join(f"  - {k} = {v!r}" for k, v in params.items()) or "  (none)"
    output_lines = "\n".join(f"  - {o['name']} ({o['type']}): {o['description']}" for o in outputs) or "  (none)"
    return f"""You are operating a legacy, server-rendered core-banking servicer console \
on behalf of a bank employee, to accomplish one specific goal. You interact with the page \
only through the tools you're given — you do not have raw browser access.

GOAL: {goal}

You are already logged in and have been navigated to: {entry_path}

Input values available for this run (use these exact values when filling fields \
that call for them — e.g. a member ID field should get the member_id value below, \
not a value you invent):
{param_lines}

You must produce these output fields before finishing (use extract_field for each):
{output_lines}

Notes on this app:
  - It is a legacy, table-based layout. There are no id or test-id attributes. Field \
labels are the adjacent table cell's text, shown to you in each element's "label".
  - Every tool call's result includes the current page state (url, page text, and the \
full list of currently visible interactive elements with their indices) — always act on \
the LATEST list of elements, never one from an earlier turn.
  - Some pages take a few seconds to load; if the page you expect isn't there yet, use \
wait_for_text rather than immediately assuming failure.
  - Some actions are genuinely irreversible (e.g. finalizing an account-opening action). If \
a button's label or context suggests this, and you intend to proceed, set on_dialog='accept' \
on that click in case it opens a native confirmation dialog. Only accept a dialog you \
actually intend to proceed with.
  - If the page shows a clear, named negative outcome (e.g. "no such member", "action not \
permitted") that is a legitimate answer, not a bug — call finish_stuck with that as the \
reason rather than trying to force the goal through. A human will decide what to do with \
that class of outcome separately; this run's job is to record the working, achievable path.
  - Work efficiently. Don't re-read the same page twice in a row without taking an action.

When the goal is genuinely achieved, call finish_success with a one-sentence summary and a \
description of what on the final page proves it (this becomes the artifact's checkpoint)."""


def _format_observation_for_model(obs, action_note: Optional[str] = None) -> str:
    lines = []
    if action_note:
        lines.append(action_note)
    lines.append(f"URL: {obs.url}")
    lines.append(f"Page text:\n{obs.page_text}")
    lines.append("Visible interactive elements:")
    for el in obs.elements:
        bits = [f"index={el.index}", f"tag={el.tag}"]
        if el.type:
            bits.append(f"type={el.type}")
        if el.name:
            bits.append(f"name={el.name!r}")
        if el.label:
            bits.append(f"label={el.label!r}")
        if el.text:
            bits.append(f"text={el.text!r}")
        if el.value:
            bits.append(f"value={el.value!r}")
        lines.append("  - " + ", ".join(bits))
    return "\n".join(lines)


class DiscoveryRun:
    def __init__(self, *, capability_name: str, goal: str, base_url: str, entry_path: str,
                 params: dict[str, str], outputs: list[dict], evidence_root: Path,
                 param_types: Optional[dict[str, str]] = None, headless: bool = True):
        self.capability_name = capability_name
        self.goal = goal
        self.base_url = base_url
        self.entry_path = entry_path
        self.params = params
        self.param_types = param_types or {}
        self.outputs = outputs
        self.headless = headless

        self.run_id = f"{capability_name}_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.evidence_dir = evidence_root / self.run_id
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.evidence_dir / "log.jsonl"

        self.allowlist = Allowlist.from_env()
        self.llm = LLMClient()

        self.recorded_steps: list[Step] = []
        self._turn = 0

    def _log(self, event: dict):
        if "tool_input" in event and isinstance(event["tool_input"], dict):
            event = {**event, "tool_input": redact_dict(event["tool_input"])}
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        with open(self._log_path, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def _bootstrap_session(self, browser: BrowserSurface):
        """Login is infrastructure, not a capability step — see module docstring."""
        browser.navigate("/login")
        obs = browser.observe("bootstrap_login")
        emp_idx = next(e.index for e in obs.elements if e.name == "employee_id")
        pass_idx = next(e.index for e in obs.elements if e.name == "passcode")
        login_idx = next(e.index for e in obs.elements if "Log In" in e.text)
        browser.fill(emp_idx, EMPLOYEE_ID, obs.elements)
        browser.fill(pass_idx, PASSCODE, obs.elements)
        browser.click(login_idx, obs.elements)
        self._log({"event": "session_bootstrapped"})

    def _record_step_for_action(self, kind: str, rec: ActionRecord) -> None:
        step_id = f"s{len(self.recorded_steps) + 1}"

        if kind == "navigate":
            self.recorded_steps.append(Step(
                step_id=step_id, intent=rec.intent, action=ActionType.NAVIGATE,
                value_literal=rec.nav_path,
            ))
            return

        if kind == "fill":
            value_param = next((name for name, val in self.params.items() if val == rec.value), None)
            self.recorded_steps.append(Step(
                step_id=step_id, intent=rec.intent, action=ActionType.FILL,
                target=LocatorSpec(strategies=derive_locator_strategies(rec.target_element)),
                value_param=value_param,
                value_literal=None if value_param else rec.value,
            ))
            return

        if kind == "click":
            irreversible = rec.dialog_message is not None
            self.recorded_steps.append(Step(
                step_id=step_id, intent=rec.intent, action=ActionType.CLICK,
                target=LocatorSpec(strategies=derive_locator_strategies(rec.target_element)),
                risk_level=RiskLevel.IRREVERSIBLE if irreversible else RiskLevel.SAFE,
                requires_confirmation=irreversible,
            ))
            if irreversible:
                dialog_step_id = f"s{len(self.recorded_steps) + 1}"
                self.recorded_steps.append(Step(
                    step_id=dialog_step_id,
                    intent=f"Handle the confirmation dialog: {rec.dialog_message!r}",
                    action=ActionType.HANDLE_DIALOG,
                    on_dialog=rec.dialog_action,
                    risk_level=RiskLevel.IRREVERSIBLE,
                    requires_confirmation=True,
                ))
            return

        if kind == "wait_for_text":
            self.recorded_steps.append(Step(
                step_id=step_id, intent=rec.intent, action=ActionType.WAIT_FOR,
                target=LocatorSpec(strategies=[LocatorStrategy(
                    method=LocatorMethod.TEXT, value=rec.value,
                    reasoning="Waiting for this exact text confirms the page has finished loading.",
                )]),
            ))
            return

        if kind == "extract_field":
            self.recorded_steps.append(Step(
                step_id=step_id, intent=rec.intent, action=ActionType.EXTRACT,
                output_name=rec.output_name,
                target=LocatorSpec(strategies=[LocatorStrategy(
                    method=LocatorMethod.XPATH, value=rec.extracted_xpath,
                    reasoning="Label-relative XPath: locates the value cell by its row's "
                    "label cell text rather than position, so row reordering doesn't break it.",
                )]),
            ))
            return

    def run(self) -> CapabilityArtifact:
        with BrowserSurface(self.base_url, self.evidence_dir, self.allowlist, headless=self.headless) as browser:
            self._bootstrap_session(browser)

            nav_rec = browser.navigate(self.entry_path)
            nav_rec.intent = "Navigate to the starting page for this capability."
            self._record_step_for_action("navigate", nav_rec)
            obs = browser.observe("initial")
            self._log({"event": "observe", "turn": 0, "url": obs.url, "n_elements": len(obs.elements)})

            system_prompt = _build_system_prompt(self.goal, self.entry_path, self.params, self.outputs)
            messages = [{"role": "user", "content": _format_observation_for_model(obs)}]

            start_time = time.time()
            extracted_outputs: dict[str, str] = {}
            finish_summary: Optional[str] = None
            checkpoint_desc: Optional[str] = None

            while True:
                self._turn += 1
                if self._turn > MAX_STEPS:
                    raise DiscoveryFailed(f"Exceeded max steps ({MAX_STEPS}) without finishing.", self.run_id)
                if time.time() - start_time > WALL_TIMEOUT_S:
                    raise DiscoveryFailed(f"Exceeded wall timeout ({WALL_TIMEOUT_S}s) without finishing.", self.run_id)

                response = self.llm.decide(system_prompt, messages)
                assistant_text = "".join(
                    b.text for b in response.content if getattr(b, "type", None) == "text"
                )
                tool_use = next((b for b in response.content if b.type == "tool_use"), None)

                self._log({
                    "event": "decide", "turn": self._turn,
                    "assistant_text": assistant_text,
                    "tool_name": tool_use.name if tool_use else None,
                    "tool_input": tool_use.input if tool_use else None,
                })

                if tool_use is None:
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": "Please call one of the provided tools."})
                    continue

                messages.append({"role": "assistant", "content": response.content})

                if tool_use.name == "finish_success":
                    finish_summary = tool_use.input.get("summary")
                    checkpoint_desc = tool_use.input.get("checkpoint_description")
                    final_url_path = browser.page.url[len(self.base_url):] or "/"
                    final_url_path = final_url_path.split("?")[0] or "/"
                    self._log({"event": "finish_success", "summary": finish_summary})
                    break

                if tool_use.name == "finish_stuck":
                    reason = tool_use.input.get("reason", "unspecified")
                    self._log({"event": "finish_stuck", "reason": reason})
                    raise DiscoveryFailed(f"Agent reported stuck: {reason}", self.run_id)

                try:
                    result_text, action_kind, rec = self._execute_tool(browser, tool_use.name, tool_use.input)
                    if action_kind is not None:
                        self._record_step_for_action(action_kind, rec)
                    if action_kind == "extract_field":
                        extracted_outputs[rec.output_name] = rec.extracted_value
                except AllowlistViolation:
                    raise
                except Exception as e:  # noqa: BLE001 - deliberately broad: feed the error back to the model
                    result_text = f"Error executing {tool_use.name}: {e}"
                    self._log({"event": "tool_error", "turn": self._turn, "error": str(e)})

                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": result_text}],
                })

            return self._build_artifact(finish_summary, checkpoint_desc, extracted_outputs, final_url_path)

    def _execute_tool(self, browser: BrowserSurface, name: str, tool_input: dict):
        if name == "navigate":
            rec = browser.navigate(tool_input["path"])
            obs = browser.observe(f"turn{self._turn}_navigate")
            return _format_observation_for_model(obs, "Navigated."), "navigate", rec

        if name == "click":
            obs = browser.observe(f"turn{self._turn}_pre_click")
            rec = browser.click(tool_input["index"], obs.elements, on_dialog=tool_input.get("on_dialog"))
            note = "Clicked."
            if rec.dialog_message:
                note += f" A confirmation dialog appeared ({rec.dialog_message!r}) and was {rec.dialog_action}ed."
            obs2 = browser.observe(f"turn{self._turn}_post_click")
            return _format_observation_for_model(obs2, note), "click", rec

        if name == "fill":
            obs = browser.observe(f"turn{self._turn}_pre_fill")
            rec = browser.fill(tool_input["index"], tool_input["value"], obs.elements)
            obs2 = browser.observe(f"turn{self._turn}_post_fill")
            return _format_observation_for_model(obs2, "Filled."), "fill", rec

        if name == "wait_for_text":
            rec = browser.wait_for_text(tool_input["text"], tool_input.get("timeout_ms", 8000))
            obs = browser.observe(f"turn{self._turn}_post_wait")
            return _format_observation_for_model(obs, "Wait condition satisfied."), "wait_for_text", rec

        if name == "extract_field":
            rec = browser.extract_field(
                tool_input["label"], tool_input["output_name"], tool_input.get("cell_index", -1)
            )
            return f"Extracted {rec.output_name} = {rec.extracted_value!r}", "extract_field", rec

        raise ValueError(f"Unknown tool: {name}")

    def _build_artifact(self, summary: Optional[str], checkpoint_desc: Optional[str],
                         extracted_outputs: dict[str, str], final_url_path: str) -> CapabilityArtifact:
        input_schema = [
            InputParam(
                name=name, type=_infer_param_type(name, value, self.param_types), required=True,
                description=f"Value for {name}.", example=str(value),
            )
            for name, value in self.params.items()
        ]
        output_schema = [
            OutputField(name=o["name"], type=ParamType(o["type"]), description=o["description"])
            for o in self.outputs
        ]

        # Checkpoint: the page the run ended on, with concrete param values
        # generalized back into {param} placeholders so it matches on replay
        # for *any* valid input, not just the one used during discovery.
        checkpoint_value = final_url_path
        for name, value in self.params.items():
            if value and value in checkpoint_value:
                checkpoint_value = checkpoint_value.replace(value, "{" + name + "}")

        artifact = CapabilityArtifact(
            artifact_id=str(uuid.uuid4()),
            name=self.capability_name,
            version="1.0.0",
            description=f"{self.goal} — {summary or ''}".strip(" —"),
            provenance=DiscoveryProvenance(
                goal=self.goal,
                discovery_run_id=self.run_id,
                model_provider="google",
                model_name=self.llm.model,
                recorded_at=datetime.now(timezone.utc),
                evidence_path=f"evidence/discovery/{self.run_id}/",
            ),
            target=TargetSurface(
                surface_type="legacy_web",
                base_url=self.base_url,
                entry_path=self.entry_path,
                vendor_product="ACME Core Servicer Terminal",
            ),
            input_schema=input_schema,
            output_schema=output_schema,
            steps=self.recorded_steps,
            checkpoint=Checkpoint(
                description=checkpoint_desc or "Final page reached without error.",
                method=CheckpointMethod.URL_MATCHES,
                value=checkpoint_value,
            ),
        )

        artifact_path = self.evidence_dir / "artifact.json"
        artifact_path.write_text(artifact.model_dump_json(indent=2))
        self._log({"event": "artifact_built", "path": str(artifact_path)})
        return artifact
