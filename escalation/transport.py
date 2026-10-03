"""
Tier 2 #9 — the control-transport interface that escalation logic depends
on, instead of depending on the concrete file-based implementation
directly.

escalation/control_channel.py's `ControlChannel` is the only implementation
that exists today, and it is still what everything in this codebase
actually uses (a JSON file polled by both sides, deliberately, because the
operator console and the CDP simulator are separate processes from the
replay run — see that module's own docstring). What changes here is that
`ControlChannel` now *implements* `ControlTransport`, and every caller that
drives escalation (replay/executor.py) is written against this ABC's
contract, not against "whatever ControlChannel happens to expose". That is
the whole point of the abstraction: swapping the file-based transport for
something else later — an HTTP-based one, so a remote operator console on
a different machine could poll/post over a network instead of sharing a
filesystem — only means writing a new `ControlTransport` subclass. Nothing
in replay/executor.py, escalation/simulated_operator.py, or
escalation/operator_console.py would need to change, because none of them
reach past this interface into file-specific details (the one exception,
by necessity, is the CDP endpoint URL itself, which is orthogonal to the
control-signal transport — that's how the operator reaches the browser,
not how the operator and the replay process exchange control state).

Sketch of what an `HttpControlTransport` would look like, without actually
building a server for this assignment: `request_intervention` etc. become
`POST /runs/{run_id}/control` calls to a small control-plane service;
`wait_for_resume` becomes a polling GET loop (or a long-poll / websocket,
if the service supports it) against the same endpoint; `status()` becomes
a GET. The state machine and exception contract below are unchanged either
way — a caller (replay/executor.py) never needs to know which transport
it's holding.

Exceptions live here, not in control_channel.py, because they're part of
the transport's *contract* (what every implementation can raise), not
specific to the file-based one:

  InterventionTimedOut  - no resume signal arrived within an optional
                          deadline. Existing behavior, unchanged.
  EscalationAbandoned    - Tier 2 #7's "cancel": an operator explicitly
                          gave up on a stuck escalation rather than letting
                          it hang forever. Raised out of wait_for_resume()
                          instead of the caller having to poll timeout vs.
                          cancel separately. A normal Exception: the caller
                          (replay/executor.py) is expected to catch this
                          and turn it into a reported FAILURE outcome —
                          it is not a crash.
  RunInterrupted         - Tier 2 #7's "interrupt": an operator ended the
                          run outright, at any point, not just while an
                          escalation is pending. Deliberately subclasses
                          BaseException, not Exception. The reason is
                          specific: replay/executor.py, discovery_loop.py,
                          and the CLI wrappers all have broad
                          `except Exception` backstops whose entire job is
                          to catch genuinely unexpected code failures
                          (a bad locator, a provider error, a crashed
                          browser) and turn them into a clean FAILURE
                          result rather than a bare traceback. "An operator
                          told this run to stop" is not a code failure, and
                          must never be silently absorbed by one of those
                          backstops and reported as if the automation
                          broke. Subclassing BaseException means those
                          `except Exception` clauses simply don't catch it
                          — it always propagates up to the one place that
                          is supposed to handle it (the explicit
                          `except RunInterrupted` in replay/executor.py's
                          own run() method), the same way KeyboardInterrupt
                          and SystemExit aren't accidentally swallowed by
                          ordinary exception handlers either.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class InterventionTimedOut(Exception):
    pass


class EscalationAbandoned(Exception):
    """An operator explicitly cancelled a pending, stuck escalation. Not a
    timeout (nobody set a deadline, or the deadline hasn't passed) — a
    deliberate "stop waiting for this" decision. The caller should report
    the run as a controlled FAILURE, not retry or keep waiting."""

    def __init__(self, reason: str = "Operator cancelled the pending intervention."):
        super().__init__(reason)
        self.reason = reason


class RunInterrupted(BaseException):
    """An operator ended the run outright. Subclasses BaseException, not
    Exception — see module docstring for why that distinction matters: it
    is the difference between "an operator stopped this" and "the code
    crashed," and a broad `except Exception` elsewhere must not be able to
    conflate the two."""

    def __init__(self, reason: str = "Operator interrupted the run."):
        super().__init__(reason)
        self.reason = reason


class InterventionKind:
    """Why a human is being brought in. Sets the default for what happens to
    the paused step on resume (see ControlTransport.signal_resume)."""

    RISK_CONFIRMATION = "risk_confirmation"  # an irreversible step needs a person to decide
    STUCK = "stuck"                          # discovery can't safely proceed
    FAILURE = "failure"                      # a replay step failed and can't recover
    TAKEOVER = "takeover"                    # the operator asked for the wheel


class ControlSignal:
    """The three signal types a transport can carry independently of the
    pause/resume handshake below (Tier 2 #7). A signal can be set at any
    time, from outside the run, and is polled by the caller at natural
    checkpoints (see replay/executor.py)."""

    TAKEOVER = "takeover"
    CANCEL = "cancel"
    INTERRUPT = "interrupt"


class ControlTransport(ABC):
    """What escalation logic is allowed to depend on. Every method here is
    already exactly what ControlChannel does today; this ABC just names the
    contract so a second implementation is possible without touching
    anything that consumes it."""

    # -- pause / resume (the original handoff handshake) -----------------

    @abstractmethod
    def request_intervention(self, *, reason: str, step_id: str, capability: str,
                              screenshot_path: str, cdp_endpoint: Optional[str],
                              intervention_request_id: str,
                              kind: str = InterventionKind.RISK_CONFIRMATION,
                              goal: Optional[str] = None, current_url: Optional[str] = None) -> None:
        """Signal that the run has stopped and is waiting for a human. Carries
        what the operator needs to act: which capability and goal, which step,
        why it stopped, where the page is, a screenshot, and how to attach."""

    @abstractmethod
    def mark_human_active(self) -> None:
        """An operator has attached and is now in control of the session."""

    @abstractmethod
    def record_human_action(self, description: str, source: str = "operator_note",
                             detail: Optional[dict] = None) -> None:
        """Append a record of what the operator did, for the run's evidence
        trail. `source` is "observed" for actions captured off the live page
        (escalation/recorder.py) and "operator_note" for the operator's own
        free-text account."""

    @abstractmethod
    def signal_resume(self, step_done: Optional[bool] = None) -> None:
        """The operator is done; automation may proceed. `step_done` says
        what happened to the step automation was paused on: True, the
        operator performed it (automation continues from the next step);
        False, they did not (automation runs it itself). None leaves it to
        the default for the intervention's kind."""

    @abstractmethod
    def wait_for_resume(self, poll_interval_s: float = 0.5, timeout_s: Optional[float] = None,
                         idle: Optional[Callable[[float], None]] = None) -> dict:
        """Block until signal_resume() is called, or until a cancel/
        interrupt signal arrives, or until timeout_s elapses. On a normal
        resume returns {"human_actions": [...recorded since this
        intervention was requested...], "step_done": bool | None}. `idle`
        replaces time.sleep between polls, so the waiting side can keep
        servicing its browser connection (see escalation/recorder.py). Raises
        InterventionTimedOut on timeout, EscalationAbandoned on cancel, or
        RunInterrupted on interrupt."""

    @abstractmethod
    def status(self) -> dict:
        """Current raw state, for the operator console / debugging."""

    # -- Tier 2 #7: distinct exit semantics, settable at any time ---------

    @abstractmethod
    def request_takeover(self, reason: str = "Operator requested manual control.") -> None:
        """An operator wants to pause the run and take the wheel *right
        now*, even though the current step wasn't flagged risky. Unlike a
        risk-triggered escalation, automation resumes the SAME step after
        the operator hands back control, rather than assuming the operator
        performed that step for it."""

    @abstractmethod
    def cancel(self, reason: str = "Operator cancelled the pending intervention.") -> None:
        """Unblock a run that is currently stuck waiting on a pending
        escalation, without waiting for a timeout. The run should report a
        controlled failure rather than hang forever."""

    @abstractmethod
    def interrupt(self, reason: str = "Operator interrupted the run.") -> None:
        """End the run outright, whether or not an escalation is pending.
        Delivered to the caller as RunInterrupted (see module docstring)."""

    @abstractmethod
    def pending_signal(self) -> Optional[dict]:
        """Non-blocking peek: {"type": ControlSignal.*, "reason": str} if
        request_takeover/cancel/interrupt was called and not yet consumed,
        else None. Polled by the caller at step boundaries so a takeover or
        interrupt can be noticed even when no escalation is in progress."""

    @abstractmethod
    def clear_signal(self) -> None:
        """Consume/clear whatever pending_signal() currently reports."""
