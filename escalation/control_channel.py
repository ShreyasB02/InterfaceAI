"""
The concrete, file-based ControlTransport implementation (see
escalation/transport.py for the interface and why it's split out). Backed
by a JSON file rather than an in-process object deliberately: the operator
console (escalation/operator_console.py) and the CDP-reattachment simulator
(escalation/simulated_operator.py) are separate processes from the replay
run, exactly like a real deployment where the automation worker and the
operator's browser session are different machines. A file both processes
can read/write, polled, is the simplest thing that actually crosses that
process boundary.

State machine (single writer at a time, statuses are the contract):
  automation           -> replay is driving the page itself
  paused_for_human      -> replay has stopped and is waiting; nobody has
                          taken over yet
  human_active          -> an operator (console or simulator) has attached
  resume_requested       -> the operator signaled they're done; replay may
                          proceed
  ended                  -> the run is over; nothing is waiting on anyone

Independent of `status` above, a `signal` field can be set at
any time by an operator to ask for one of three things without waiting for
`status` to reach a particular value first:
  takeover requested   -> operator wants to pause and take control right
                          now, even on a step that wasn't flagged risky
  cancel requested      -> operator wants to give up on a pending, stuck
                          escalation rather than let it hang forever
  interrupt requested   -> operator wants the run to end outright

`signal` and `status` are deliberately two different fields: `status` is
the pause/resume handshake's own state, while `signal` is an out-of-band
request an operator can make regardless of what `status` currently is. See
escalation/transport.py's ControlTransport for the full contract.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from escalation.transport import (
    ControlSignal,
    ControlTransport,
    EscalationAbandoned,
    InterventionKind,
    InterventionTimedOut,
    RunInterrupted,
)

# Re-exported for any existing caller importing these from this module
# rather than from escalation.transport directly (backward compatible).
__all__ = [
    "ControlChannel",
    "InterventionTimedOut",
    "EscalationAbandoned",
    "RunInterrupted",
]


class ControlChannel(ControlTransport):
    def __init__(self, run_id: str, evidence_dir: Path):
        self.run_id = run_id
        self.path = evidence_dir / "control.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"run_id": run_id, "status": "automation", "human_actions": []})

    def _write(self, state: dict) -> None:
        # Write-then-atomic-rename rather than an in-place write_text(): two
        # separate processes/threads poll and write this same file (the
        # operator console or CDP simulator vs. the replay run's own
        # wait_for_resume loop), and signal polling adds more of that traffic
        # (pending_signal() is now polled every step, not just while
        # already escalated). An in-place write is not atomic — a reader
        # can observe a truncated/partial file mid-write and fail to parse
        # it. Writing to a sibling temp file and rename()-ing it into place
        # is atomic on POSIX, so a concurrent reader always sees either the
        # complete old file or the complete new one, never a torn one.
        tmp_path = self.path.with_name(self.path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
        tmp_path.write_text(json.dumps(state, indent=2, default=str))
        tmp_path.replace(self.path)

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    # -- pause / resume ---------------------------------------------------

    def request_intervention(self, *, reason: str, step_id: str, capability: str,
                              screenshot_path: str, cdp_endpoint: Optional[str],
                              intervention_request_id: str,
                              kind: str = InterventionKind.RISK_CONFIRMATION,
                              goal: Optional[str] = None, current_url: Optional[str] = None) -> None:
        state = self._read()
        state["status"] = "paused_for_human"
        state.pop("step_done", None)
        state["intervention_request"] = {
            "id": intervention_request_id,
            "kind": kind,
            "reason": reason,
            "step_id": step_id,
            "capability": capability,
            "goal": goal,
            "current_url": current_url,
            # human_actions is one list for the whole run; this marks where
            # this intervention's entries begin.
            "actions_from": len(state.get("human_actions", [])),
            "screenshot_path": screenshot_path,
            "cdp_endpoint": cdp_endpoint,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(state)

    def mark_human_active(self) -> None:
        state = self._read()
        state["status"] = "human_active"
        self._write(state)

    def record_human_action(self, description: str, source: str = "operator_note",
                             detail: Optional[dict] = None) -> None:
        state = self._read()
        entry = {"at": datetime.now(timezone.utc).isoformat(), "source": source, "description": description}
        entry.update(detail or {})
        state.setdefault("human_actions", []).append(entry)
        self._write(state)

    def signal_resume(self, step_done: Optional[bool] = None) -> None:
        state = self._read()
        state["status"] = "resume_requested"
        state["step_done"] = step_done
        self._write(state)

    def wait_for_resume(self, poll_interval_s: float = 0.5, timeout_s: Optional[float] = None,
                         idle: Optional[Callable[[float], None]] = None) -> dict:
        start = time.time()
        idle = idle or time.sleep
        while True:
            state = self._read()

            signal = state.get("signal")
            if signal:
                sig_type = signal.get("type")
                reason = signal.get("reason") or ""
                if sig_type == ControlSignal.INTERRUPT:
                    state.pop("signal", None)
                    state["status"] = "automation"
                    self._write(state)
                    raise RunInterrupted(reason or "Operator interrupted the run.")
                if sig_type == ControlSignal.CANCEL:
                    state.pop("signal", None)
                    state["status"] = "automation"
                    self._write(state)
                    raise EscalationAbandoned(reason or "Operator cancelled the pending intervention.")
                # A takeover signal received while already escalated is a
                # no-op here — there's nothing more to take over.

            if state.get("status") == "resume_requested":
                state["status"] = "automation"
                self._write(state)
                actions_from = (state.get("intervention_request") or {}).get("actions_from", 0)
                return {"human_actions": state.get("human_actions", [])[actions_from:],
                        "step_done": state.get("step_done")}

            if timeout_s is not None and (time.time() - start) > timeout_s:
                raise InterventionTimedOut(f"No resume signal within {timeout_s}s for run {self.run_id}")
            idle(poll_interval_s)

    def end(self, outcome: str) -> None:
        state = self._read()
        state["status"] = "ended"
        state["outcome"] = outcome
        state.pop("signal", None)
        self._write(state)

    def status(self) -> dict:
        return self._read()

    # -- takeover / cancel / interrupt --------------------------

    def request_takeover(self, reason: str = "Operator requested manual control.") -> None:
        state = self._read()
        state["signal"] = {"type": ControlSignal.TAKEOVER, "reason": reason}
        self._write(state)

    def cancel(self, reason: str = "Operator cancelled the pending intervention.") -> None:
        state = self._read()
        state["signal"] = {"type": ControlSignal.CANCEL, "reason": reason}
        self._write(state)

    def interrupt(self, reason: str = "Operator interrupted the run.") -> None:
        state = self._read()
        state["signal"] = {"type": ControlSignal.INTERRUPT, "reason": reason}
        self._write(state)

    def pending_signal(self) -> Optional[dict]:
        return self._read().get("signal")

    def clear_signal(self) -> None:
        state = self._read()
        state.pop("signal", None)
        self._write(state)
