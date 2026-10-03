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
    ReplayResult,
    Step,
)
from artifacts.validate import validate_required_params
from escalation.control_channel import ControlChannel
from escalation.transport import ControlSignal, EscalationAbandoned, InterventionTimedOut, RunInterrupted
from guardrails.allowlist import Allowlist, AllowlistViolation
from guardrails.redaction import redact_dict, redact_text
from guardrails.risk_policy import needs_escalation
from guardrails.safety import safe_screenshot
from replay.locator_resolver import LocatorResolutionError, ResolvedLocator, build_locator, describe, resolve

EMPLOYEE_ID = "EMP001"
PASSCODE = "demo1234"
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

    # -- logging / evidence ----------------------------------------------

    def _log(self, event: dict):
        if "tool_input" in event and isinstance(event["tool_input"], dict):
            event = {**event, "tool_input": redact_dict(event["tool_input"])}
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
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
        page.goto(base_url.rstrip("/") + "/login")
        page.locator('input[name="employee_id"]').fill(EMPLOYEE_ID)
        page.locator('input[name="passcode"]').fill(PASSCODE)
        page.get_by_role("button", name="Log In").click()
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
            # their own dialog listener attached. Don't race them.
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
            page.goto(self._base_url.rstrip("/") + "/" + self._resolve_nav_path(step, params).lstrip("/"))
            self.allowlist.check_url(page.url)
        elif step.action == ActionType.FILL:
            resolved = resolve(page, step.target, timeout_ms=step.timeout_ms)
            resolved.locator.fill(str(self._resolve_value(step, params)))
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

    def run(self, params: dict, base_url: Optional[str] = None, auto_approve: bool = False,
            escalation_timeout_s: Optional[float] = None,
            on_escalation: Optional[Callable[[ControlChannel, dict], None]] = None) -> ReplayResult:
        started_at = datetime.now(timezone.utc)
        self._base_url = base_url or self.artifact.target.base_url

        err = validate_required_params(self.artifact, params)
        if err:
            return ReplayResult(
                outcome=ReplayOutcome.INPUT_ERROR, artifact_id=self.artifact.artifact_id,
                artifact_version=self.artifact.version, run_id=self.run_id,
                started_at=started_at, finished_at=datetime.now(timezone.utc),
                failure={"step_id": "preflight", "expected": "valid input params",
                         "observed": err, "message": err},
                evidence_path=f"evidence/replay/{self.run_id}/",
            )

        self.allowlist.check_url(self._base_url.rstrip("/") + "/")
        self._log({"event": "run_started", "artifact": self.artifact.name,
                    "version": self.artifact.version, "params": redact_dict(params)})

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

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless, args=[f"--remote-debugging-port={self.cdp_port}"])
            page = browser.new_page()
            page.on("dialog", self._on_dialog)

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

                    # Tier 2 #7: an interrupt can land at any step boundary,
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

                    # Tier 2 #7: a manual takeover request stands in for the
                    # guardrail-driven `needs_escalation` check below — an
                    # operator can grab the wheel on ANY step, not only a
                    # CLICK the artifact flagged risky.
                    manual_takeover = bool(sig) and sig.get("type") == ControlSignal.TAKEOVER
                    risky_click = step.action == ActionType.CLICK and needs_escalation(step, auto_approve)

                    if manual_takeover or risky_click:
                        control.clear_signal()  # no-op if no signal was pending
                        shot = self._screenshot(page, f"pre_escalation_{step.step_id}")
                        req_id = str(uuid.uuid4())
                        if manual_takeover and not risky_click:
                            reason = sig.get("reason") or "Operator requested manual takeover."
                        else:
                            reason = (f"Step {step.step_id} ({step.intent}) is irreversible and requires "
                                      "human confirmation before proceeding.")
                        control.request_intervention(
                            reason=reason, step_id=step.step_id, capability=self.artifact.name,
                            screenshot_path=shot, cdp_endpoint=f"http://127.0.0.1:{self.cdp_port}",
                            intervention_request_id=req_id,
                        )
                        self._log({"event": "escalation_requested", "step_id": step.step_id,
                                    "request_id": req_id, "manual_takeover": manual_takeover})

                        self._escalation_in_progress = True
                        if on_escalation:
                            on_escalation(control, {"step_id": step.step_id, "run_dir": str(self.evidence_dir)})

                        try:
                            human_actions = control.wait_for_resume(timeout_s=escalation_timeout_s)
                        except InterventionTimedOut:
                            outcome = ReplayOutcome.ESCALATED
                            escalation = {"reason": "Timed out waiting for a human to resolve the "
                                          "intervention.", "step_id": step.step_id,
                                          "intervention_request_id": req_id}
                            break
                        except EscalationAbandoned as e:
                            # Tier 2 #7's "cancel": an operator explicitly gave
                            # up on this pending intervention rather than
                            # leaving it to hang forever or wait out a
                            # timeout. Reported as a controlled FAILURE, not
                            # a hang and not a silent retry.
                            outcome = ReplayOutcome.FAILURE
                            failure = {"step_id": step.step_id, "expected": "a human to resolve the intervention",
                                       "observed": "the pending escalation was cancelled by an operator",
                                       "message": str(e)}
                            self._log({"event": "escalation_cancelled", "step_id": step.step_id, "detail": str(e)})
                            break
                        finally:
                            self._escalation_in_progress = False

                        escalation = {"reason": reason, "step_id": step.step_id, "intervention_request_id": req_id}
                        self._log({"event": "escalation_resumed", "human_actions": human_actions})

                        if risky_click:
                            # The human performed the click (and any dialog)
                            # themselves on the live session. Skip this step
                            # and its paired HANDLE_DIALOG step; continue from
                            # whatever comes next.
                            i += 1
                            if i < len(steps) and steps[i].action == ActionType.HANDLE_DIALOG:
                                i += 1
                            last_step_id = step.step_id
                            steps_executed += 1
                            continue

                        # Manual takeover on a step automation still owns:
                        # the operator handed control back rather than
                        # performing the step itself, so fall through and
                        # let automation execute this same step now.

                    # Pre-register dialog handling if the NEXT step says how
                    # to handle a dialog this click is expected to trigger.
                    self._pending_dialog_action = None
                    self._pending_dialog_message = None
                    if step.action == ActionType.CLICK and i + 1 < len(steps) and steps[i + 1].action == ActionType.HANDLE_DIALOG:
                        self._pending_dialog_action = steps[i + 1].on_dialog

                    resolved = self._execute_step(page, step, params, outputs)
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
                # Tier 2 #7's "interrupt". Caught explicitly, right here —
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
