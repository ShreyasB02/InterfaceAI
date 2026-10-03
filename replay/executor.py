"""
The deterministic replay engine — the production execution path. No LLM
call anywhere in this file. Given a saved CapabilityArtifact and a set of
input params, it drives the browser using ONLY the artifact's recorded
steps and ranked locator strategies, and returns a ReplayResult whose
`outcome` is one of: INPUT_ERROR (bad params, checked before the browser
opens), SUCCESS, BUSINESS_OUTCOME, or FAILURE — plus ESCALATED for a run
that paused for a human and wasn't resumed before an optional timeout.
See artifacts/schema/result.py for the full rationale on each.

Known-outcome and recoverable-pattern checks run with a short timeout
(a few hundred ms) rather than the step timeout, deliberately: they're a
"is this already here" probe on the happy path, not something we want to
sit around waiting for on every single step.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import Dialog, Error as PlaywrightError, sync_playwright

from artifacts.coerce import coerce_output_value
from artifacts.schema import (
    ActionType,
    CapabilityArtifact,
    CheckpointMethod,
    InterruptDetail,
    OutcomeMarker,
    RecoverablePattern,
    RecoveryAction,
    ReplayOutcome,
    RiskLevel,
    ReplayResult,
    Step,
)
from artifacts.validate import validate_required_params
from escalation.control_channel import ControlChannel
from escalation.recorder import HumanActionRecorder
from escalation.transport import (
    ControlSignal,
    EscalationAbandoned,
    InterventionKind,
    InterventionTimedOut,
    RunInterrupted,
)
from guardrails.allowlist import Allowlist, AllowlistViolation
from guardrails.credentials import operator_credentials
from guardrails.network import NetworkGuard, RiskyActionBlocked
from guardrails.redaction import redact_dict, redact_text
from guardrails.risk_policy import needs_escalation, replay_refusal
from guardrails.safety import safe_screenshot
from replay.locator_resolver import LocatorResolutionError, ResolvedLocator, build_locator, describe, resolve

DEFAULT_CDP_PORT = 9333
OUTCOME_PROBE_TIMEOUT_MS = 400


class StepAssertionFailed(Exception):
    """A mid-flow ASSERT_TEXT step did not hold. Carries expected/observed
    so the FailureDetail is debuggable without re-running."""

    def __init__(self, expected: str, observed: str):
        super().__init__(f"Expected {expected}; observed {observed}")
        self.expected = expected
        self.observed = observed


class ReplayExecutor:
    def __init__(self, artifact: CapabilityArtifact, evidence_root: Path,
                 headless: bool = True, cdp_port: int = DEFAULT_CDP_PORT):
        self.artifact = artifact
        self.headless = headless
        self.cdp_port = cdp_port
        self.allowlist = Allowlist.from_env()

        self.run_id = f"replay_{artifact.name}_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.evidence_dir = evidence_root / self.run_id
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        (self.evidence_dir / "screenshots").mkdir(exist_ok=True)
        self._log_path = self.evidence_dir / "log.jsonl"
        self._shot_count = 0

        self.step_lookup: dict[str, Step] = {s.step_id: s for s in artifact.steps}
        self._pending_dialog_action: Optional[str] = None
        self._pending_dialog_message: Optional[str] = None
        self._escalation_in_progress = False
        self.recorder = HumanActionRecorder()

    # -- logging / evidence ----------------------------------------------

    def _log(self, event: dict):
        # Every field of every event, at every depth — not only the ones
        # remembered to be sensitive.
        event = {"ts": datetime.now(timezone.utc).isoformat(), **redact_dict(event)}
        with open(self._log_path, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def _screenshot(self, page, tag: str) -> str:
        self._shot_count += 1
        name = f"{self._shot_count:03d}_{tag}.png"
        safe_screenshot(page, str(self.evidence_dir / "screenshots" / name))
        return f"screenshots/{name}"

    def _failure(self, page, step_id: Optional[str], expected: str, observed: str,
                 message: str, event: str = "step_failed") -> dict:
        """Builds a FailureDetail and captures the richer failure evidence
        (screenshot + redacted DOM snapshot). Capture is best-effort: the
        page may be the thing that broke."""
        shot = dom = None
        try:
            shot = self._screenshot(page, "failure")
            (self.evidence_dir / "failure_dom.html").write_text(redact_text(page.content()))
            dom = "failure_dom.html"
        except Exception:  # noqa: BLE001 - evidence capture must never mask the real failure
            pass
        failure = {"step_id": step_id or "unknown", "expected": expected, "observed": observed,
                   "message": message, "screenshot": shot, "dom_snapshot": dom}
        self._log({"event": event, **failure})
        return failure

    @staticmethod
    def _safe_url(page) -> str:
        try:
            return page.url
        except Exception:  # noqa: BLE001
            return "<unavailable>"

    def _resolve_value(self, step: Step, params: dict):
        if step.value_param:
            return params.get(step.value_param)
        return step.value_literal

    def _resolve_nav_path(self, step: Step, params: dict) -> str:
        path = step.value_literal or "/"
        for name, value in params.items():
            path = path.replace("{" + name + "}", str(value))
        return path

    # -- session bootstrap (infrastructure, not a recorded step) ---------

    def _bootstrap_session(self, page, base_url: str):
        employee_id, passcode = operator_credentials()
        login_url = base_url.rstrip("/") + "/login"
        self.allowlist.check_url(login_url)
        self.guard.begin()
        page.goto(login_url)
        page.locator('input[name="employee_id"]').fill(employee_id)
        page.locator('input[name="passcode"]').fill(passcode)
        page.get_by_role("button", name="Log In").click()
        self.guard.raise_if_blocked()
        self.allowlist.check_url(page.url)
        self._log({"event": "session_bootstrapped"})

    # -- outcome / recovery detection -------------------------------------

    def _quick_detect(self, page, spec) -> bool:
        for strategy in spec.strategies:
            try:
                loc = build_locator(page, strategy)
                loc.first.wait_for(state="visible", timeout=OUTCOME_PROBE_TIMEOUT_MS)
                if loc.count() >= 1:
                    return True
            except Exception:  # noqa: BLE001 - a miss on a probe is not an error
                continue
        return False

    def _check_known_outcomes(self, page, after_step: str) -> Optional[OutcomeMarker]:
        for marker in self.artifact.known_outcomes:
            if marker.after_step == after_step and self._quick_detect(page, marker.detection):
                return marker
        return None

    def _check_recoverable(self, page, after_step: str) -> Optional[RecoverablePattern]:
        for pattern in self.artifact.recoverable_patterns:
            if pattern.after_step == after_step and self._quick_detect(page, pattern.detection):
                return pattern
        return None

    def _apply_recovery(self, page, pattern: RecoverablePattern, params: dict, outputs: dict):
        if pattern.recovery_action == RecoveryAction.RELOAD_AND_RETRY:
            page.reload()
        elif pattern.recovery_action == RecoveryAction.RETRY_STEP:
            producing = self.step_lookup[pattern.after_step]
            self._execute_step(page, producing, params, outputs)
        elif pattern.recovery_action == RecoveryAction.DISMISS_AND_CONTINUE:
            pass  # condition detected but doesn't block proceeding

    # -- dialog handling ----------------------------------------------------

    def _on_dialog(self, dialog: Dialog):
        if self._escalation_in_progress:
            # A human (or the simulated operator, via a separate CDP
            # connection) is in control of this session right now and has
            # their own dialog listener attached. Don't race them — just
            # note that it appeared, as part of what happened on their watch.
            self.recorder.note_dialog(dialog.message)
            return
        self._pending_dialog_message = dialog.message
        action = self._pending_dialog_action or "dismiss"
        try:
            (dialog.accept if action == "accept" else dialog.dismiss)()
        except Exception:
            # Benign race: the operator's separate CDP connection resolved
            # this same browser-side dialog microseconds before our own
            # listener got scheduled. Nothing left for us to do.
            pass

    # -- step execution ----------------------------------------------------

    def _execute_step(self, page, step: Step, params: dict, outputs: dict) -> Optional[ResolvedLocator]:
        """Executes one step. Returns the ResolvedLocator for steps that
        target an element, so the caller can record which ranked strategy
        actually matched (the drift signal)."""
        resolved: Optional[ResolvedLocator] = None
        if step.action == ActionType.NAVIGATE:
            url = self._base_url.rstrip("/") + "/" + self._resolve_nav_path(step, params).lstrip("/")
            self.allowlist.check_url(url)  # before leaving, not after arriving
            page.goto(url)
            self.allowlist.check_url(page.url)
        elif step.action == ActionType.FILL:
            value = self._resolve_value(step, params)
            if value is None:
                # A value a human typed during discovery that isn't a declared
                # param is never stored. Such a step is handed to a human.
                raise StepAssertionFailed("a value for this fill step",
                                          "the artifact records none (it was entered by a human during discovery)")
            resolved = resolve(page, step.target, timeout_ms=step.timeout_ms)
            resolved.locator.fill(str(value))
        elif step.action == ActionType.CLICK:
            resolved = resolve(page, step.target, timeout_ms=step.timeout_ms)
            resolved.locator.click()
            self.allowlist.check_url(page.url)
        elif step.action == ActionType.WAIT_FOR:
            resolved = resolve(page, step.target, timeout_ms=step.timeout_ms)
        elif step.action == ActionType.ASSERT_TEXT:
            resolved = self._assert_text(page, step, params)
        elif step.action == ActionType.EXTRACT:
            resolved = resolve(page, step.target, timeout_ms=step.timeout_ms)
            outputs[step.output_name] = resolved.locator.inner_text().strip()
        elif step.action == ActionType.HANDLE_DIALOG:
            pass  # consumed by the preceding CLICK step, see run()
        return resolved

    def _assert_text(self, page, step: Step, params: dict) -> Optional[ResolvedLocator]:
        """Mid-flow checkpoint. With a target: that element's text must
        contain the expected value. Without one: the text must be visible
        somewhere on the page."""
        expected = self._resolve_value(step, params)
        if expected is None:
            raise StepAssertionFailed("an expected text value on the step", "assert_text step has no value")
        expected = str(expected)
        if step.target is not None:
            resolved = resolve(page, step.target, timeout_ms=step.timeout_ms)
            actual = resolved.locator.inner_text().strip()
            if expected not in actual:
                raise StepAssertionFailed(f"target text to contain {expected!r}", f"{redact_text(actual)[:200]!r}")
            return resolved
        try:
            page.get_by_text(expected, exact=False).first.wait_for(state="visible", timeout=step.timeout_ms)
        except PlaywrightError:
            raise StepAssertionFailed(f"text {expected!r} visible on the page",
                                      f"not visible within {step.timeout_ms}ms (url={self._safe_url(page)})")
        return None

    # -- main entry point ----------------------------------------------------

    def _handoff(self, page, control: ControlChannel, step: Step, *, kind: str, reason: str,
                 params: dict, on_escalation, timeout_s: Optional[float]) -> dict:
        """Pause, cede the live session to a human, and wait to get it back.

        Control-transfer model: from request_intervention() until
        wait_for_resume() returns, the human owns the session. Automation
        performs no page action in that window — it only idles, receiving
        the recorder's observations of what the human does. Ownership is
        the control channel's `status` field, readable by anyone.

        Returns {"status": "resumed" | "timed_out" | "cancelled",
                 "detail": <EscalationDetail fields>, "step_done": bool | None,
                 "error": str | None}.
        """
        shot = self._screenshot(page, f"pre_escalation_{step.step_id}")
        req_id = str(uuid.uuid4())
        control.request_intervention(
            reason=reason, step_id=step.step_id, capability=self.artifact.name,
            screenshot_path=shot, cdp_endpoint=f"http://127.0.0.1:{self.cdp_port}",
            intervention_request_id=req_id, kind=kind,
            goal=self.artifact.description, current_url=self._safe_url(page),
        )
        self._log({"event": "escalation_requested", "step_id": step.step_id, "request_id": req_id,
                   "kind": kind, "reason": reason, "screenshot": shot})

        status, error, resume = "resumed", None, {}
        self._escalation_in_progress = True
        # The human may perform risky actions — that is what they are here
        # for — but the allowlist still binds the session they are driving.
        self.guard.begin(allow_risky=True)
        self.recorder.start()
        try:
            if on_escalation:
                on_escalation(control, {"step_id": step.step_id, "run_dir": str(self.evidence_dir), "kind": kind})
            resume = control.wait_for_resume(timeout_s=timeout_s, idle=self.recorder.pump)
        except InterventionTimedOut as e:
            status, error = "timed_out", str(e)
        except EscalationAbandoned as e:
            status, error = "cancelled", str(e)
        finally:
            # Also runs when RunInterrupted propagates: control is taken
            # back and recording stops whichever way the wait ended.
            observed = self.recorder.stop()
            self._escalation_in_progress = False

        observed_evidence = [a.to_evidence(params) for a in observed]
        for entry in observed_evidence:
            control.record_human_action(entry["description"], source="observed",
                                        detail={k: v for k, v in entry.items()
                                                if k not in ("at", "source", "description")})
        human_actions = observed_evidence + resume.get("human_actions", [])
        step_done = resume.get("step_done")
        self._log({"event": f"escalation_{status}", "step_id": step.step_id, "request_id": req_id,
                   "step_done": step_done, "human_actions": human_actions,
                   "requests_blocked_by_policy": list(self.guard.violations),
                   "screenshot": self._screenshot(page, f"post_escalation_{step.step_id}")})
        return {"status": status, "error": error, "step_done": step_done,
                "detail": {"reason": reason, "step_id": step.step_id, "intervention_request_id": req_id,
                           "kind": kind, "resolution": status, "human_actions": human_actions}}

    @staticmethod
    def _skip_step(steps: list[Step], i: int) -> int:
        """Index after step i, also passing the HANDLE_DIALOG paired with it:
        the human who performed the step dealt with its dialog too."""
        i += 1
        if i < len(steps) and steps[i].action == ActionType.HANDLE_DIALOG:
            i += 1
        return i

    @staticmethod
    def _handoff_ended(handoff: dict, step: Step) -> Optional[tuple]:
        """(outcome, failure) if the handoff ended the run, else None."""
        if handoff["status"] == "timed_out":
            return ReplayOutcome.ESCALATED, None
        if handoff["status"] == "cancelled":
            return ReplayOutcome.FAILURE, {
                "step_id": step.step_id, "expected": "a human to resolve the intervention",
                "observed": "the pending escalation was cancelled by an operator",
                "message": handoff["error"]}
        return None

    def _preflight_result(self, outcome: ReplayOutcome, started_at: datetime, expected: str,
                          observed: str) -> ReplayResult:
        """A run that is turned away before the browser opens. Still leaves
        a log line and a result.json, so a refusal is as auditable as a run."""
        result = ReplayResult(
            outcome=outcome, artifact_id=self.artifact.artifact_id,
            artifact_version=self.artifact.version, run_id=self.run_id,
            started_at=started_at, finished_at=datetime.now(timezone.utc),
            failure={"step_id": "preflight", "expected": expected, "observed": observed, "message": observed},
            evidence_path=f"evidence/replay/{self.run_id}/",
        )
        self._log({"event": "run_not_started", "outcome": outcome.value, "artifact": self.artifact.name,
                   "version": self.artifact.version, "reason": observed})
        (self.evidence_dir / "result.json").write_text(result.model_dump_json(indent=2))
        return result

    def run(self, params: dict, base_url: Optional[str] = None, auto_approve: bool = False,
            attended: bool = False, escalate_on_failure: bool = False,
            escalation_timeout_s: Optional[float] = None,
            on_escalation: Optional[Callable[[ControlChannel, dict], None]] = None) -> ReplayResult:
        started_at = datetime.now(timezone.utc)
        self._base_url = base_url or self.artifact.target.base_url

        # Policy first: is this artifact allowed to run this way at all?
        # Enforced here, not in the CLI, so no caller can route around it.
        refusal = replay_refusal(self.artifact, attended=attended, auto_approve=auto_approve)
        if refusal:
            return self._preflight_result(ReplayOutcome.REFUSED, started_at,
                                          "an artifact approved for this kind of run", refusal)

        err = validate_required_params(self.artifact, params)
        if err:
            return self._preflight_result(ReplayOutcome.INPUT_ERROR, started_at, "valid input params", err)

        self.allowlist.check_url(self._base_url.rstrip("/") + "/")
        self._log({"event": "run_started", "artifact": self.artifact.name,
                    "version": self.artifact.version, "status": self.artifact.status.value,
                    "attended": attended, "auto_approve": auto_approve, "params": redact_dict(params)})

        control = ControlChannel(self.run_id, self.evidence_dir)
        outputs: dict = {}
        recovered_steps = []
        locator_fallbacks = []
        last_step_id: Optional[str] = None
        current_step_id: Optional[str] = None
        outcome = ReplayOutcome.SUCCESS
        business_outcome = None
        failure = None
        escalation = None
        interrupted = None
        steps_executed = 0

        self.guard = NetworkGuard(self.allowlist)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless, args=[f"--remote-debugging-port={self.cdp_port}"])
            page = browser.new_page()
            page.on("dialog", self._on_dialog)
            self.recorder.install(page)
            self.guard.install(page.context)
            escalated_failures: set[str] = set()

            try:
                # Inside the try on purpose: a failed/slow login is a hard
                # failure the caller needs as a structured result, not a
                # traceback.
                current_step_id = "session_bootstrap"
                self._bootstrap_session(page, self._base_url)

                i = 0
                steps = self.artifact.steps
                while i < len(steps):
                    step = steps[i]
                    current_step_id = step.step_id

                    # An interrupt can land at any step boundary,
                    # not just while an escalation is already pending — check
                    # it before anything else this iteration does. Raised as
                    # RunInterrupted (a BaseException, not Exception — see
                    # escalation/transport.py) so it can only be caught by
                    # the explicit handler below, never by a broad
                    # `except Exception` elsewhere mistaking an operator's
                    # stop for a code failure.
                    sig = control.pending_signal()
                    if sig and sig.get("type") == ControlSignal.INTERRUPT:
                        control.clear_signal()
                        reason = sig.get("reason") or "Operator interrupted the run."
                        self._log({"event": "run_interrupted", "step_id": step.step_id, "reason": reason})
                        raise RunInterrupted(reason)

                    bo = self._check_known_outcomes(page, last_step_id) if last_step_id else None
                    if bo:
                        business_outcome = {"code": bo.code, "message": bo.message}
                        outcome = ReplayOutcome.BUSINESS_OUTCOME
                        self._log({"event": "business_outcome", "code": bo.code})
                        break

                    rp = self._check_recoverable(page, last_step_id) if last_step_id else None
                    if rp:
                        for attempt in range(1, rp.max_attempts + 1):
                            self._log({"event": "recovering", "condition": rp.condition, "attempt": attempt})
                            self._apply_recovery(page, rp, params, outputs)
                            if not self._quick_detect(page, rp.detection):
                                recovered_steps.append({
                                    "step_id": step.step_id, "condition": rp.condition,
                                    "action_taken": f"{rp.recovery_action.value} (attempt {attempt})",
                                })
                                break
                        else:
                            failure = {"step_id": step.step_id, "expected": "recoverable condition resolved",
                                       "observed": f"'{rp.condition}' persisted after {rp.max_attempts} attempt(s)",
                                       "message": "Recoverable pattern exhausted its retry budget."}
                            outcome = ReplayOutcome.FAILURE
                            break

                    # Two reasons to hand this step to a human before running
                    # it: the artifact says it needs one (an irreversible
                    # click, or a value only a human supplied), or an
                    # operator asked for the wheel — which they can do on ANY
                    # step, not only one flagged risky.
                    manual_takeover = bool(sig) and sig.get("type") == ControlSignal.TAKEOVER
                    needs_human = (step.action in (ActionType.CLICK, ActionType.FILL)
                                   and needs_escalation(step, auto_approve))

                    if manual_takeover or needs_human:
                        control.clear_signal()  # no-op if no signal was pending
                        if needs_human:
                            kind = InterventionKind.RISK_CONFIRMATION
                            reason = (f"Step {step.step_id} ({step.intent}) is {step.risk_level.value} and "
                                      "requires a human before proceeding.")
                        else:
                            kind = InterventionKind.TAKEOVER
                            reason = sig.get("reason") or "Operator requested manual takeover."
                        handoff = self._handoff(page, control, step, kind=kind, reason=reason, params=params,
                                                on_escalation=on_escalation, timeout_s=escalation_timeout_s)
                        escalation = handoff["detail"]
                        ended = self._handoff_ended(handoff, step)
                        if ended:
                            outcome, failure = ended
                            break

                        # Did the human perform the paused step? Their answer
                        # if they gave one; otherwise yes for a step that was
                        # theirs to do, no for a look-around takeover.
                        step_done = handoff["step_done"] if handoff["step_done"] is not None else needs_human
                        if step_done:
                            i = self._skip_step(steps, i)
                            last_step_id = step.step_id
                            steps_executed += 1
                            continue

                    # Pre-register dialog handling if the NEXT step says how
                    # to handle a dialog this click is expected to trigger.
                    self._pending_dialog_action = None
                    self._pending_dialog_message = None
                    if step.action == ActionType.CLICK and i + 1 < len(steps) and steps[i + 1].action == ActionType.HANDLE_DIALOG:
                        self._pending_dialog_action = steps[i + 1].on_dialog

                    try:
                        self.allowlist.check_action(step.action.value)
                        # A step reaching this point with a risk flag has been
                        # cleared: a human handed it back to run, or the
                        # artifact's review stands as its confirmation
                        # (--auto-approve). Any other step is held to safe
                        # requests only, whatever the artifact claims.
                        self.guard.begin(allow_risky=step.requires_confirmation
                                         and step.risk_level in (RiskLevel.RISKY, RiskLevel.IRREVERSIBLE))
                        resolved = self._execute_step(page, step, params, outputs)
                        self.guard.raise_if_blocked()
                    except (LocatorResolutionError, StepAssertionFailed, PlaywrightError, RiskyActionBlocked) as e:
                        # Opt-in: instead of failing, bring a human to the
                        # live session at the point of failure. Once per
                        # step — if it fails again after the handoff, that
                        # is a hard failure (handled below, as before).
                        if not escalate_on_failure or step.step_id in escalated_failures:
                            raise
                        escalated_failures.add(step.step_id)
                        reason = (f"Step {step.step_id} ({step.intent}) failed and replay cannot recover "
                                  f"on its own: {str(e).splitlines()[0][:300]}")
                        self._log({"event": "step_failed_escalating", "step_id": step.step_id, "error": reason})
                        handoff = self._handoff(page, control, step, kind=InterventionKind.FAILURE, reason=reason,
                                                params=params, on_escalation=on_escalation,
                                                timeout_s=escalation_timeout_s)
                        escalation = handoff["detail"]
                        ended = self._handoff_ended(handoff, step)
                        if ended:
                            outcome, failure = ended
                            break
                        if handoff["step_done"]:
                            i = self._skip_step(steps, i)
                            last_step_id = step.step_id
                            steps_executed += 1
                        # Otherwise the human cleared the obstacle and handed
                        # the step back: loop round and run it again.
                        continue

                    event = {"event": "step_executed", "step_id": step.step_id,
                             "action": step.action.value, "intent": step.intent}
                    if resolved is not None:
                        event["locator_strategy_index"] = resolved.strategy_index
                        event["locator_method"] = resolved.method.value
                        if resolved.strategy_index > 0:
                            locator_fallbacks.append({"step_id": step.step_id,
                                                      "strategy_index": resolved.strategy_index,
                                                      "method": resolved.method.value})
                    self._log(event)
                    steps_executed += 1

                    if step.action == ActionType.CLICK and i + 1 < len(steps) and steps[i + 1].action == ActionType.HANDLE_DIALOG:
                        i += 1  # consume the paired HANDLE_DIALOG step too
                        steps_executed += 1

                    last_step_id = steps[i].step_id
                    i += 1

                # A known outcome can also follow the LAST step (there is no
                # "next iteration" to check it in).
                if outcome == ReplayOutcome.SUCCESS and last_step_id:
                    bo = self._check_known_outcomes(page, last_step_id)
                    if bo:
                        business_outcome = {"code": bo.code, "message": bo.message}
                        outcome = ReplayOutcome.BUSINESS_OUTCOME
                        self._log({"event": "business_outcome", "code": bo.code})

                if outcome == ReplayOutcome.SUCCESS and not self._verify_checkpoint(page, params):
                    outcome = ReplayOutcome.FAILURE
                    failure = self._failure(
                        page, "checkpoint", expected=self.artifact.checkpoint.description,
                        observed=f"url={self._safe_url(page)}",
                        message="All steps executed without error, but the declared checkpoint "
                                "was not satisfied afterward.", event="checkpoint_failed")

                # SUCCESS promises the declared outputs. One that was never
                # extracted, or doesn't coerce to its declared type, is a
                # broken contract — not a success with a hole in it.
                if outcome == ReplayOutcome.SUCCESS:
                    bad = [f.name for f in self.artifact.output_schema
                           if f.name not in outputs or coerce_output_value(outputs[f.name], f.type) is None]
                    if bad:
                        outcome = ReplayOutcome.FAILURE
                        failure = self._failure(
                            page, "outputs", expected=f"declared outputs {[f.name for f in self.artifact.output_schema]}",
                            observed=f"missing or not coercible to declared type: {bad}",
                            message="Checkpoint held, but the run did not produce every declared output.",
                            event="outputs_incomplete")

            except RunInterrupted as e:
                # "interrupt". Caught explicitly, right here —
                # this is the one place that owns turning "an operator
                # stopped this" into a clean, typed result. Everywhere else
                # in this codebase that has a broad `except Exception`
                # backstop is safe from ever intercepting this instead,
                # precisely because RunInterrupted subclasses BaseException
                # (see escalation/transport.py).
                try:
                    shot = self._screenshot(page, "interrupted")
                except Exception:
                    shot = None
                outcome = ReplayOutcome.INTERRUPTED
                interrupted = InterruptDetail(step_id=current_step_id or "unknown", reason=e.reason)
                self._log({"event": "run_interrupted_caught", "detail": e.reason, "screenshot": shot})
            except LocatorResolutionError as e:
                outcome = ReplayOutcome.FAILURE
                failure = self._failure(
                    page, current_step_id,
                    expected="exactly one visible element matching one of: "
                             + " | ".join(describe(st) for st in e.spec.strategies),
                    observed="; ".join(e.attempts) + f" (url={self._safe_url(page)})",
                    message=str(e), event="locator_resolution_failed")
            except StepAssertionFailed as e:
                outcome = ReplayOutcome.FAILURE
                failure = self._failure(page, current_step_id, expected=e.expected, observed=e.observed,
                                        message="A mid-flow assertion did not hold.", event="assertion_failed")
            except RiskyActionBlocked as e:
                outcome = ReplayOutcome.FAILURE
                failure = self._failure(
                    page, current_step_id, expected="a step recorded as safe to make only safe requests",
                    observed=str(e), message="Risk policy violation — the artifact under-classifies this step.",
                    event="risky_request_blocked")
            except AllowlistViolation as e:
                outcome = ReplayOutcome.FAILURE
                failure = self._failure(page, current_step_id, expected="URL within allowlist", observed=str(e),
                                        message="Allowlist violation — execution stopped.",
                                        event="allowlist_violation")
            except PlaywrightError as e:
                # Timeouts, failed navigations (app down, connection reset),
                # a crashed page: the "slow/failed load" and "outright app
                # error" class. Hard failure, but a structured one.
                step = self.step_lookup.get(current_step_id or "")
                outcome = ReplayOutcome.FAILURE
                failure = self._failure(
                    page, current_step_id,
                    expected=step.intent if step else "session bootstrap (login) to complete",
                    observed=f"{type(e).__name__}: {str(e).splitlines()[0]} (url={self._safe_url(page)})",
                    message="A browser action failed or timed out.", event="browser_action_failed")
            except Exception as e:  # noqa: BLE001 - last-resort backstop; RunInterrupted is a
                # BaseException and is handled above, so it can never land here.
                outcome = ReplayOutcome.FAILURE
                failure = self._failure(
                    page, current_step_id, expected="step to execute without an internal error",
                    observed=f"{type(e).__name__}: {e}",
                    message="Unexpected error inside the replay engine.", event="unexpected_error")

            try:
                final_shot = self._screenshot(page, f"final_{outcome.value}")
                self._log({"event": "final_state", "outcome": outcome.value, "screenshot": final_shot})
            except Exception:
                pass  # page may already be in a bad state; not worth failing the run over

            browser.close()

        typed_outputs = {}
        for field in self.artifact.output_schema:
            if field.name in outputs:
                typed_outputs[field.name] = coerce_output_value(outputs[field.name], field.type)

        result = ReplayResult(
            outcome=outcome, artifact_id=self.artifact.artifact_id, artifact_version=self.artifact.version,
            run_id=self.run_id, started_at=started_at, finished_at=datetime.now(timezone.utc),
            outputs=typed_outputs, business_outcome=business_outcome, failure=failure, escalation=escalation,
            interrupted=interrupted, recovered_steps=recovered_steps, locator_fallbacks=locator_fallbacks,
            steps_executed=steps_executed,
            evidence_path=f"evidence/replay/{self.run_id}/",
        )
        (self.evidence_dir / "result.json").write_text(result.model_dump_json(indent=2))
        self._log({"event": "run_finished", "outcome": outcome.value})
        return result

    def _verify_checkpoint(self, page, params: dict) -> bool:
        cp = self.artifact.checkpoint
        if cp.method == CheckpointMethod.URL_MATCHES:
            expected = cp.value
            for name, value in params.items():
                expected = expected.replace("{" + name + "}", str(value))
            return expected in page.url
        if cp.method == CheckpointMethod.TEXT_PRESENT:
            try:
                page.get_by_text(cp.value, exact=False).first.wait_for(state="visible", timeout=2000)
                return True
            except Exception:
                return False
        if cp.method == CheckpointMethod.ELEMENT_PRESENT:
            try:
                resolve(page, cp.target, timeout_ms=2000)
                return True
            except LocatorResolutionError:
                return False
        return False
