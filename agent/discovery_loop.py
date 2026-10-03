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
from agent.llm import LLMClient
from agent.locator_inference import ElementMeta, derive_locator_strategies
from escalation.control_channel import ControlChannel
from escalation.recorder import ObservedAction
from escalation.transport import (
    ControlSignal,
    EscalationAbandoned,
    InterventionKind,
    InterventionTimedOut,
    RunInterrupted,
)
from guardrails.allowlist import Allowlist, AllowlistViolation
from guardrails.credentials import operator_credentials
from guardrails.network import RiskyActionBlocked
from guardrails.redaction import (
    generalize_for_artifact,
    redact_dict,
    redact_field_value,
    redact_text,
    scrub_known_value,
)
from guardrails.tokenizer import TOKEN_EXPLAINER, Tokenizer


MAX_STEPS = 25
WALL_TIMEOUT_S = 180  # the model's working time; time spent waiting on a human doesn't count

# "Stuck" as the harness sees it, independent of the model saying so:
STUCK_REPEAT_THRESHOLD = 3   # the same tool call with the same input, this many times running
STUCK_ERROR_THRESHOLD = 3    # this many consecutive tool calls that raised
MAX_ESCALATIONS = 3          # a run that needs a human more often than this isn't a capability


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
                          outputs: list[dict], tokenizer: Tokenizer, vision: bool) -> str:
    # Param VALUES are tokenized (see guardrails/tokenizer.py) before ever
    # landing in this prompt — an ordinary-looking ID like a member_id
    # won't match any sensitive shape and passes through unchanged, but a
    # param that happens to look like a card/account/SSN/dollar-amount
    # value is shown to the model only as a placeholder.
    param_lines = "\n".join(f"  - {k} = {tokenizer.tokenize(str(v))!r}" for k, v in params.items()) or "  (none)"
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
  - {"Alongside that text description, you are also shown a current screenshot of the page "
     "each turn. Use it to visually confirm layout, disambiguate elements that look similar "
     "in the text list, and notice anything the text description alone might miss."
     if vision else
     "You are not shown a screenshot — rely on the text description and element list only."}
  - {TOKEN_EXPLAINER}
  - Some pages take a few seconds to load; if the page you expect isn't there yet, use \
wait_for_text rather than immediately assuming failure.
  - Some actions are genuinely irreversible (e.g. finalizing an account-opening action). If \
a button's label or context suggests this, and you intend to proceed, set on_dialog='accept' \
on that click in case it opens a native confirmation dialog. Only accept a dialog you \
actually intend to proceed with.
  - If you cannot safely proceed but a person could — the goal leaves a decision to someone \
else, you cannot find the control you need, or the screen is not what you expected — call \
request_human with the reason. A human operator takes over this same session, acts, and \
hands it back; you then continue from the new page state. Do not guess in their place.
  - If the page shows a clear, named negative outcome (e.g. "no such member", "action not \
permitted") that is a legitimate answer, not a bug, and not something a human could fix \
either — call finish_stuck with that as the reason rather than trying to force the goal through.
  - Work efficiently. Don't re-read the same page twice in a row without taking an action.

When the goal is genuinely achieved, call finish_success with a one-sentence summary and a \
description of what on the final page proves it (this becomes the artifact's checkpoint)."""


def _append_to_last_user_message(messages: list[dict], text: str) -> None:
    """Add text to the latest user turn instead of opening a second
    consecutive user turn, which not every provider accepts."""
    last = messages[-1]
    if isinstance(last["content"], str):
        last["content"] += "\n\n" + text
    else:
        last["content"][-1]["content"] += "\n\n" + text


def _format_observation_for_model(obs, tokenizer: Tokenizer, action_note: Optional[str] = None) -> str:
    # Page-derived text is tokenized (see guardrails/tokenizer.py) before it
    # ever becomes part of the model's context — this is the boundary where
    # raw page content would otherwise reach the LLM. Structural fields
    # (tag/type/name/label) aren't tokenized: they're the app's own field
    # naming, not scraped values.
    lines = []
    if action_note:
        lines.append(tokenizer.tokenize(action_note))
    lines.append(f"URL: {obs.url}")
    lines.append(f"Page text:\n{tokenizer.tokenize(obs.page_text)}")
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
            bits.append(f"text={tokenizer.tokenize(el.text)!r}")
        if el.value:
            bits.append(f"value={tokenizer.tokenize(el.value)!r}")
        lines.append("  - " + ", ".join(bits))
    return "\n".join(lines)


class DiscoveryRun:
    def __init__(self, *, capability_name: str, goal: str, base_url: str, entry_path: str,
                 params: dict[str, str], outputs: list[dict], evidence_root: Path,
                 param_types: Optional[dict[str, str]] = None, headless: bool = True,
                 vision: bool = True, llm=None, artifact_version: str = "1.0.0",
                 escalation_timeout_s: Optional[float] = 600, on_escalation=None, cdp_port: int = 9334,
                 risk_gate: bool = True):
        self.capability_name = capability_name
        self.goal = goal
        self.base_url = base_url
        self.entry_path = entry_path
        self.params = params
        self.param_types = param_types or {}
        self.outputs = outputs
        self.headless = headless
        # Vision: attach a real screenshot to the model's context each turn
        # (agent/llm/), on top of the existing text/DOM-derived
        # observation. Kept togglable — a provider outage on the image
        # path, or a deliberate text-only comparison run, shouldn't require
        # code changes.
        self.vision = vision
        self.artifact_version = artifact_version

        self.run_id = f"{capability_name}_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.evidence_dir = evidence_root / self.run_id
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.evidence_dir / "log.jsonl"

        self.allowlist = Allowlist.from_env()
        # Any object with decide()/model works here; the default is the
        # provider router built from env (agent/llm/router.py). Its retry
        # and failover events go into this run's evidence log.
        self.llm = llm or LLMClient.from_env()
        if hasattr(self.llm, "on_event"):
            self.llm.on_event = self._log
        # One tokenizer per run — see guardrails/tokenizer.py.
        # Its token map is purely in-memory and is discarded with this
        # object; nothing about it is ever written to evidence.
        self.tokenizer = Tokenizer()

        self.recorded_steps: list[Step] = []
        self._turn = 0
        # raw extracted value -> label, for scrubbing logs (see _log)
        self._record_values: dict[str, str] = {}
        # Irreversible actions need a human's approval during discovery too.
        self.risk_gate = risk_gate
        self._last_handoff: dict = {}

        # Human handoff (see _escalate). Same control channel and file layout
        # replay uses, so the operator console works on a discovery run too.
        self.control = ControlChannel(self.run_id, self.evidence_dir)
        self.escalation_timeout_s = escalation_timeout_s
        self.on_escalation = on_escalation
        self.cdp_port = cdp_port
        self.interventions: list[dict] = []
        self._human_wait_s = 0.0

    def _read_screenshot_bytes(self, relative_path: Optional[str]) -> Optional[bytes]:
        if not relative_path:
            return None
        try:
            return (self.evidence_dir / relative_path).read_bytes()
        except OSError:
            # Screenshot capture is already best-effort (safe_screenshot());
            # if the file isn't there, just run this turn without vision
            # rather than failing the whole run over it.
            return None

    def _log(self, event: dict):
        # Two passes over the whole event. Value shapes (account numbers,
        # amounts, secrets) at every depth; then literal values this run
        # read off a record — a member's name has no shape to match, but we
        # know it's record data because we extracted it.
        event = scrub_known_value(redact_dict(event), self._record_values)
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        with open(self._log_path, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def _bootstrap_session(self, browser: BrowserSurface):
        """Login is infrastructure, not a capability step — see module
        docstring. Driven directly on the page, outside the tool surface:
        it isn't recorded, isn't logged, never reaches the model, and isn't
        subject to the action-type allowlist (a read-only policy must still
        be able to sign in). The domain/route policy does apply."""
        employee_id, passcode = operator_credentials()
        login_url = self.base_url.rstrip("/") + "/login"
        self.allowlist.check_url(login_url)
        browser.guard.begin()
        browser.page.goto(login_url)
        browser.page.locator('input[name="employee_id"]').fill(employee_id)
        browser.page.locator('input[name="passcode"]').fill(passcode)
        browser.page.get_by_role("button", name="Log In").click()
        browser.guard.raise_if_blocked()
        self.allowlist.check_url(browser.page.url)
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
            # A typed value that isn't one of the declared inputs is kept as
            # a literal only if nothing about it looks sensitive (a constant
            # like a dropdown choice). Otherwise it is not stored at all,
            # and the step is handed to a human at replay.
            el = rec.target_element
            literal_is_safe = redact_field_value(el.name, el.type, rec.value or "") == (rec.value or "")
            keep_literal = value_param is None and literal_is_safe
            unstorable = value_param is None and not literal_is_safe
            self.recorded_steps.append(Step(
                step_id=step_id, intent=rec.intent, action=ActionType.FILL,
                target=LocatorSpec(strategies=derive_locator_strategies(el)),
                value_param=value_param,
                value_literal=rec.value if keep_literal else None,
                risk_level=RiskLevel.RISKY if unstorable else RiskLevel.SAFE,
                requires_confirmation=unstorable,
            ))
            return

        if kind == "click":
            # Two independent signals, either is enough: the app asked "are
            # you sure?" with a native dialog, or the click made a
            # state-changing request to a route policy marks risky.
            irreversible = rec.dialog_message is not None or rec.risky_request
            self.recorded_steps.append(Step(
                step_id=step_id, intent=rec.intent, action=ActionType.CLICK,
                target=LocatorSpec(strategies=derive_locator_strategies(rec.target_element)),
                risk_level=RiskLevel.IRREVERSIBLE if irreversible else RiskLevel.SAFE,
                requires_confirmation=irreversible,
            ))
            if rec.dialog_message is not None:
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
        with BrowserSurface(self.base_url, self.evidence_dir, self.allowlist, headless=self.headless,
                            cdp_port=self.cdp_port) as browser:
            self._bootstrap_session(browser)

            nav_rec = browser.navigate(self.entry_path)
            nav_rec.intent = "Navigate to the starting page for this capability."
            self._record_step_for_action("navigate", nav_rec)
            obs = browser.observe("initial")
            self._log({"event": "observe", "turn": 0, "url": obs.url, "n_elements": len(obs.elements)})

            system_prompt = _build_system_prompt(
                self.goal, self.entry_path, self.params, self.outputs, self.tokenizer, self.vision
            )
            messages = [{"role": "user", "content": _format_observation_for_model(obs, self.tokenizer)}]
            latest_screenshot = self._read_screenshot_bytes(obs.screenshot_path) if self.vision else None

            start_time = time.time()
            extracted_outputs: dict[str, str] = {}
            finish_summary: Optional[str] = None
            checkpoint_desc: Optional[str] = None
            last_call: Optional[tuple] = None
            repeat_count = 0
            error_streak = 0

            while True:
                self._turn += 1
                if self._turn > MAX_STEPS:
                    raise DiscoveryFailed(f"Exceeded max steps ({MAX_STEPS}) without finishing.", self.run_id)
                if time.time() - start_time - self._human_wait_s > WALL_TIMEOUT_S:
                    raise DiscoveryFailed(f"Exceeded wall timeout ({WALL_TIMEOUT_S}s) without finishing.", self.run_id)

                # An operator can stop the run, or ask for the wheel, at any
                # turn boundary — not only when the run itself asks for help.
                sig = self.control.pending_signal()
                if sig:
                    self.control.clear_signal()
                    if sig.get("type") == ControlSignal.INTERRUPT:
                        self._log({"event": "run_interrupted", "turn": self._turn, "reason": sig.get("reason")})
                        raise RunInterrupted(sig.get("reason") or "Operator interrupted the run.")
                    if sig.get("type") == ControlSignal.TAKEOVER:
                        note, obs = self._escalate(browser, sig.get("reason") or "Operator requested manual takeover.",
                                                   InterventionKind.TAKEOVER)
                        _append_to_last_user_message(messages, note)
                        if self.vision:
                            latest_screenshot = self._read_screenshot_bytes(obs.screenshot_path)

                try:
                    response = self.llm.decide(system_prompt, messages, image_bytes=latest_screenshot)
                except Exception as e:
                    # Reached only once the router has exhausted its
                    # retries on every configured provider (agent/llm/
                    # router.py). Still must not surface as a bare
                    # traceback: turn it into the same clean, evidence-
                    # logged failure as every other way a run can end.
                    self._log({"event": "llm_call_failed", "turn": self._turn, "error": str(e)})
                    raise DiscoveryFailed(f"LLM call failed: {type(e).__name__}: {e}", self.run_id) from e

                assistant_text = "".join(
                    b.text for b in response.content if getattr(b, "type", None) == "text"
                )
                tool_use = next((b for b in response.content if b.type == "tool_use"), None)

                self._log({
                    "event": "decide", "turn": self._turn,
                    "provider": getattr(response, "provider", None),
                    "model": getattr(response, "model", None) or self.llm.model,
                    "assistant_text": assistant_text,
                    "tool_name": tool_use.name if tool_use else None,
                    "tool_input": tool_use.input if tool_use else None,
                    "vision_attached": latest_screenshot is not None,
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

                if tool_use.name == "request_human":
                    note, obs = self._escalate(browser, tool_use.input.get("reason", "unspecified"),
                                               InterventionKind.STUCK)
                    messages.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": tool_use.id, "content": note}]})
                    if self.vision:
                        latest_screenshot = self._read_screenshot_bytes(obs.screenshot_path)
                    last_call, repeat_count, error_streak = None, 0, 0
                    continue

                # Stuck without saying so: the same call, with the same input,
                # over and over. Bring a human in rather than burn the step
                # budget (or do the same possibly-harmful thing a fourth time).
                call = (tool_use.name, json.dumps(tool_use.input, sort_keys=True, default=str))
                repeat_count = repeat_count + 1 if call == last_call else 1
                last_call = call
                if repeat_count >= STUCK_REPEAT_THRESHOLD:
                    note, obs = self._escalate(
                        browser, f"No progress: the agent issued the same {tool_use.name} call "
                        f"{repeat_count} times in a row.", InterventionKind.STUCK)
                    messages.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": tool_use.id,
                         "content": "That call was not executed — you were repeating yourself. " + note}]})
                    if self.vision:
                        latest_screenshot = self._read_screenshot_bytes(obs.screenshot_path)
                    last_call, repeat_count, error_streak = None, 0, 0
                    continue

                if tool_use.name == "finish_stuck":
                    reason = tool_use.input.get("reason", "unspecified")
                    self._log({"event": "finish_stuck", "reason": reason})
                    raise DiscoveryFailed(f"Agent reported stuck: {reason}", self.run_id)

                try:
                    result_text, action_kind, rec, obs_for_shot = self._execute_tool(
                        browser, tool_use.name, tool_use.input
                    )
                    if action_kind is not None:
                        self._record_step_for_action(action_kind, rec)
                    if action_kind == "extract_field":
                        extracted_outputs[rec.output_name] = rec.extracted_value
                        if rec.extracted_value:
                            self._record_values[rec.extracted_value] = f"[REDACTED-{rec.output_name}]"
                    if self.vision and obs_for_shot is not None:
                        latest_screenshot = self._read_screenshot_bytes(obs_for_shot.screenshot_path)
                    error_streak = 0
                except AllowlistViolation:
                    raise
                except Exception as e:  # noqa: BLE001 - deliberately broad: feed the error back to the model
                    result_text = f"Error executing {tool_use.name}: {e}"
                    if isinstance(e, RiskyActionBlocked):
                        result_text += (" If you do intend this irreversible action, call click again with "
                                        "on_dialog='accept': a human will be asked to approve it first.")
                    self._log({"event": "tool_error", "turn": self._turn, "error": str(e)})
                    error_streak += 1
                    if error_streak >= STUCK_ERROR_THRESHOLD:
                        note, obs = self._escalate(
                            browser, f"No progress: {error_streak} tool calls in a row failed. Last error: "
                            f"{str(e).splitlines()[0][:200]}", InterventionKind.STUCK)
                        result_text += "\n" + note
                        if self.vision:
                            latest_screenshot = self._read_screenshot_bytes(obs.screenshot_path)
                        last_call, repeat_count, error_streak = None, 0, 0

                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": result_text}],
                })

            return self._build_artifact(finish_summary, checkpoint_desc, extracted_outputs, final_url_path)

    # -- human handoff ---------------------------------------------------

    def _escalate(self, browser: BrowserSurface, reason: str, kind: str):
        """Hand the live discovery session to a human and take it back.

        Same control-transfer model as replay (replay/executor.py's
        _handoff): from request_intervention() until wait_for_resume()
        returns, the human owns the session and this loop performs no page
        action — it only idles, receiving the recorder's observations.

        What the human does is recorded twice over: as evidence, and as
        artifact steps (origin="human"), so a flow that needed a person is
        still captured end to end. Returns (note for the model, the fresh
        observation it was built from).
        """
        if len(self.interventions) >= MAX_ESCALATIONS:
            raise DiscoveryFailed(
                f"Needed a human more than {MAX_ESCALATIONS} times; giving up. Last reason: {reason}", self.run_id)

        shot = browser.screenshot(f"turn{self._turn}_pre_escalation")
        req_id = str(uuid.uuid4())
        self.control.request_intervention(
            reason=reason, step_id=f"turn{self._turn}", capability=self.capability_name,
            screenshot_path=shot, cdp_endpoint=browser.cdp_endpoint, intervention_request_id=req_id,
            kind=kind, goal=self.goal, current_url=browser.page.url,
        )
        self._log({"event": "escalation_requested", "turn": self._turn, "request_id": req_id, "kind": kind,
                   "reason": reason, "screenshot": shot, "recorded_steps_so_far": len(self.recorded_steps)})

        waited_from = time.time()
        browser.guard.begin(allow_risky=True)  # the human may act on risk; the allowlist still binds them
        browser.recorder.start()
        try:
            if self.on_escalation:
                self.on_escalation(self.control, {"turn": self._turn, "run_dir": str(self.evidence_dir), "kind": kind})
            resume = self.control.wait_for_resume(timeout_s=self.escalation_timeout_s, idle=browser.recorder.pump)
        except InterventionTimedOut as e:
            self._log({"event": "escalation_timed_out", "request_id": req_id, "detail": str(e)})
            raise DiscoveryFailed(f"Escalated to a human ({reason}) but nobody resolved it: {e}", self.run_id) from e
        except EscalationAbandoned as e:
            self._log({"event": "escalation_cancelled", "request_id": req_id, "detail": str(e)})
            raise DiscoveryFailed(f"Escalation cancelled by an operator: {e}", self.run_id) from e
        finally:
            observed = browser.recorder.stop()
            self._human_wait_s += time.time() - waited_from

        observed_evidence = [a.to_evidence(self.params) for a in observed]
        for entry in observed_evidence:
            self.control.record_human_action(
                entry["description"], source="observed",
                detail={k: v for k, v in entry.items() if k not in ("at", "source", "description")})
        human_actions = observed_evidence + resume.get("human_actions", [])
        new_steps = self._record_human_steps(observed)
        self._last_handoff = {"observed": observed, "step_done": resume.get("step_done")}

        obs = browser.observe(f"turn{self._turn}_post_escalation")
        self.interventions.append({"request_id": req_id, "kind": kind, "reason": reason,
                                   "turn": self._turn, "human_actions": human_actions})
        self._log({"event": "escalation_resumed", "turn": self._turn, "request_id": req_id,
                   "human_actions": human_actions, "steps_recorded_from_human": new_steps,
                   "screenshot": obs.screenshot_path})

        did = "; ".join(a.describe(self.params) for a in observed if a.kind != "navigate") or "nothing on the page"
        note = ("A human operator took control of this session and has handed it back. They did: "
                f"{self.tokenizer.tokenize(did)}. Those actions are already recorded — do not repeat them. "
                "Continue toward the goal from the current page state:\n"
                + _format_observation_for_model(obs, self.tokenizer))
        return note, obs

    def _record_human_steps(self, observed: list[ObservedAction]) -> list[str]:
        """Turn what the operator did into artifact steps, with the same
        locator inference the model's own actions get."""
        new_ids: list[str] = []
        for n, action in enumerate(observed):
            if action.kind not in ("click", "fill"):
                continue
            el = ElementMeta(index=-1, tag=action.tag, type=action.type, name=action.name,
                             value=action.text if action.tag == "input" else "", text=action.text,
                             label=action.label)
            step_id = f"s{len(self.recorded_steps) + 1}"
            target = LocatorSpec(strategies=derive_locator_strategies(el))
            intent = f"{action.describe(self.params)} (performed by a human operator during discovery)"

            if action.kind == "fill":
                value_param = next((k for k, v in self.params.items() if str(v) == (action.value or "")), None)
                # A value the human typed that isn't a declared input is
                # never written to the artifact. The step is kept, flagged,
                # and replay hands it to a human rather than inventing one.
                self.recorded_steps.append(Step(
                    step_id=step_id, intent=intent, action=ActionType.FILL, target=target, origin="human",
                    value_param=value_param,
                    risk_level=RiskLevel.SAFE if value_param else RiskLevel.RISKY,
                    requires_confirmation=value_param is None,
                ))
                new_ids.append(step_id)
                continue

            # A click that raised a native dialog on the human's watch is
            # irreversible by the same rule the model's clicks follow.
            later = observed[n + 1:]
            next_input = next((i for i, a in enumerate(later) if a.kind in ("click", "fill")), len(later))
            dialog = next((a for a in later[:next_input] if a.kind == "dialog"), None)
            self.recorded_steps.append(Step(
                step_id=step_id, intent=intent, action=ActionType.CLICK, target=target, origin="human",
                risk_level=RiskLevel.IRREVERSIBLE if dialog else RiskLevel.SAFE,
                requires_confirmation=dialog is not None,
            ))
            new_ids.append(step_id)
            if dialog:
                navigated = any(a.kind == "navigate" for a in later[:next_input])
                dialog_id = f"s{len(self.recorded_steps) + 1}"
                self.recorded_steps.append(Step(
                    step_id=dialog_id, intent=f"Handle the confirmation dialog: {dialog.message!r}",
                    action=ActionType.HANDLE_DIALOG, on_dialog="accept" if navigated else "dismiss",
                    origin="human", risk_level=RiskLevel.IRREVERSIBLE, requires_confirmation=True,
                ))
                new_ids.append(dialog_id)
        return new_ids

    def _execute_tool(self, browser: BrowserSurface, name: str, tool_input: dict):
        """Returns (result_text, action_kind, rec, obs_for_screenshot). The
        4th element is the freshest Observation produced by this call (or
        None if the tool didn't re-observe the page), purely so run() can
        pull the next turn's screenshot bytes off it — it's not otherwise
        part of the model-facing contract.

        Detokenization happens here, right before a token would drive a
        real browser action (fill's value, wait_for_text's text) — the one
        boundary where a placeholder minted by guardrails/tokenizer.py is
        turned back into the real value it stands for. Everywhere else in
        this file stays on the tokenized side of that boundary.
        """
        if name == "navigate":
            rec = browser.navigate(tool_input["path"])
            obs = browser.observe(f"turn{self._turn}_navigate")
            return _format_observation_for_model(obs, self.tokenizer, "Navigated."), "navigate", rec, obs

        if name == "click":
            obs = browser.observe(f"turn{self._turn}_pre_click")
            # on_dialog='accept' is the model declaring "I expect this to be
            # irreversible and I mean to go through with it".
            declared_irreversible = tool_input.get("on_dialog") == "accept"
            if declared_irreversible and self.risk_gate:
                el = next((e for e in obs.elements if e.index == tool_input["index"]), None)
                target = el.describe() if el else f"element {tool_input['index']}"
                handoff_note, obs_after = self._escalate(
                    browser, f"The agent is about to perform an action it expects to be irreversible: "
                    f"click {target}. Hand back for automation to do it, do it yourself, or cancel.",
                    InterventionKind.RISK_CONFIRMATION)
                handoff = self._last_handoff
                human_did_it = (handoff["step_done"] if handoff["step_done"] is not None
                                else any(a.kind == "click" for a in handoff["observed"]))
                if human_did_it:
                    return handoff_note, None, None, obs_after
                self._log({"event": "risky_action_approved", "turn": self._turn, "target": target})
                obs = browser.observe(f"turn{self._turn}_pre_click_approved")
            rec = browser.click(tool_input["index"], obs.elements, on_dialog=tool_input.get("on_dialog"),
                                allow_risky=declared_irreversible)
            note = "Clicked."
            if rec.dialog_message:
                note += f" A confirmation dialog appeared ({rec.dialog_message!r}) and was {rec.dialog_action}ed."
            obs2 = browser.observe(f"turn{self._turn}_post_click")
            return _format_observation_for_model(obs2, self.tokenizer, note), "click", rec, obs2

        if name == "fill":
            obs = browser.observe(f"turn{self._turn}_pre_fill")
            raw_value = self.tokenizer.detokenize(tool_input["value"])
            rec = browser.fill(tool_input["index"], raw_value, obs.elements)
            obs2 = browser.observe(f"turn{self._turn}_post_fill")
            return _format_observation_for_model(obs2, self.tokenizer, "Filled."), "fill", rec, obs2

        if name == "wait_for_text":
            raw_text = self.tokenizer.detokenize(tool_input["text"])
            rec = browser.wait_for_text(raw_text, tool_input.get("timeout_ms", 8000))
            obs = browser.observe(f"turn{self._turn}_post_wait")
            return _format_observation_for_model(obs, self.tokenizer, "Wait condition satisfied."), \
                "wait_for_text", rec, obs

        if name == "extract_field":
            # Labels are the app's static field names (e.g. "Savings"), not
            # scraped values, so they're never tokenized on the way out —
            # detokenizing here is just defensive symmetry in case one ever
            # is. The extracted VALUE (rec.extracted_value) always comes
            # straight off the real DOM via browser.extract_field() and is
            # what actually lands in the artifact's outputs — only the
            # echo shown back to the model is tokenized below.
            raw_label = self.tokenizer.detokenize(tool_input["label"])
            rec = browser.extract_field(
                raw_label, tool_input["output_name"], tool_input.get("cell_index", -1)
            )
            shown_value = self.tokenizer.tokenize(rec.extracted_value)
            return f"Extracted {rec.output_name} = {shown_value!r}", "extract_field", rec, None

        raise ValueError(f"Unknown tool: {name}")

    def _build_artifact(self, summary: Optional[str], checkpoint_desc: Optional[str],
                         extracted_outputs: dict[str, str], final_url_path: str) -> CapabilityArtifact:
        input_schema = [
            InputParam(
                name=name, type=_infer_param_type(name, value, self.param_types), required=True,
                description=f"Value for {name}.", example=redact_text(str(value)),
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

        # The model wrote the summary and checkpoint description while looking
        # at one member's record, and quotes it. An artifact describes the
        # capability, not that record.
        def clean(text: str) -> str:
            return generalize_for_artifact(text, self.params, extracted_outputs)

        artifact = CapabilityArtifact(
            artifact_id=str(uuid.uuid4()),
            name=self.capability_name,
            version=self.artifact_version,
            description=clean(f"{self.goal} — {summary or ''}".strip(" —")),
            provenance=DiscoveryProvenance(
                goal=clean(self.goal),
                discovery_run_id=self.run_id,
                model_provider=getattr(self.llm, "provider", "unknown"),
                model_name=self.llm.model,
                recorded_at=datetime.now(timezone.utc),
                evidence_path=f"evidence/discovery/{self.run_id}/",
                human_interventions=len(self.interventions),
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
                description=clean(checkpoint_desc or "Final page reached without error."),
                method=CheckpointMethod.URL_MATCHES,
                value=checkpoint_value,
            ),
        )

        artifact_path = self.evidence_dir / "artifact.json"
        artifact_path.write_text(artifact.model_dump_json(indent=2))
        self._log({"event": "artifact_built", "path": str(artifact_path)})
        return artifact
