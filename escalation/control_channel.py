"""
The control-transfer channel between an in-flight replay and a human
operator. Backed by a JSON file rather than an in-process object
deliberately: the operator console (escalation/operator_console.py) and the
CDP-reattachment simulator (escalation/simulated_operator.py) are separate
processes from the replay run, exactly like a real deployment where the
automation worker and the operator's browser session are different
machines. A file both processes can read/write, polled, is the simplest
thing that actually crosses that process boundary.

State machine (single writer at a time, statuses are the contract):
  automation          -> replay is driving the page itself
  paused_for_human     -> replay has stopped and is waiting; nobody has
                          taken over yet
  human_active         -> an operator (console or simulator) has attached
  resume_requested      -> the operator signaled they're done; replay may
                          proceed
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class InterventionTimedOut(Exception):
    pass


class ControlChannel:
    def __init__(self, run_id: str, evidence_dir: Path):
        self.run_id = run_id
        self.path = evidence_dir / "control.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"run_id": run_id, "status": "automation", "human_actions": []})

    def _write(self, state: dict) -> None:
        self.path.write_text(json.dumps(state, indent=2, default=str))

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def request_intervention(self, *, reason: str, step_id: str, capability: str,
                              screenshot_path: str, cdp_endpoint: Optional[str],
                              intervention_request_id: str) -> None:
        state = self._read()
        state["status"] = "paused_for_human"
        state["intervention_request"] = {
            "id": intervention_request_id,
            "reason": reason,
            "step_id": step_id,
            "capability": capability,
            "screenshot_path": screenshot_path,
            "cdp_endpoint": cdp_endpoint,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(state)

    def mark_human_active(self) -> None:
        state = self._read()
        state["status"] = "human_active"
        self._write(state)

    def record_human_action(self, description: str) -> None:
        state = self._read()
        state.setdefault("human_actions", []).append({
            "at": datetime.now(timezone.utc).isoformat(),
            "description": description,
        })
        self._write(state)

    def signal_resume(self) -> None:
        state = self._read()
        state["status"] = "resume_requested"
        self._write(state)

    def wait_for_resume(self, poll_interval_s: float = 0.5, timeout_s: Optional[float] = None) -> list[dict]:
        start = time.time()
        while True:
            state = self._read()
            if state.get("status") == "resume_requested":
                state["status"] = "automation"
                self._write(state)
                return state.get("human_actions", [])
            if timeout_s is not None and (time.time() - start) > timeout_s:
                raise InterventionTimedOut(f"No resume signal within {timeout_s}s for run {self.run_id}")
            time.sleep(poll_interval_s)

    def status(self) -> dict:
        return self._read()
