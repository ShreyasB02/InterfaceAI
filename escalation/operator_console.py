"""
Bare mock operator console — the minimal-but-real UI a human uses to take
over a paused replay run's live session. Out of scope per the brief is a
full real-time co-browsing view; what's real here is that this reads and
writes the same control-channel file the replay executor is blocked on, so
clicking "Resume Automation" here genuinely un-blocks a running replay
process (possibly on a different machine).

To actually drive the paused page (rather than just recording what you did
in the textarea), open chrome://inspect in a real Chrome, add the CDP
endpoint shown here as a "Network target", and click "inspect" — that
reattaches to the exact same live page replay was on, the same mechanism
escalation/simulated_operator.py uses programmatically for repeatable
evidence.

Tier 2 #7 adds three controls beyond the original pause/resume pair, all
driven through the same ControlTransport interface (escalation/transport.py)
rather than anything console-specific:
  - "Take Over Now", available even while the run shows status=automation
    (no pending intervention yet) — requests a voluntary pause on whatever
    step the run is about to execute next, regardless of that step's own
    risk classification.
  - "Cancel" on a pending intervention — gives up on it immediately rather
    than waiting out a timeout; the run reports a controlled FAILURE.
  - "Interrupt Run" — ends the run outright, at any point, whether or not
    an escalation is currently pending.
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, redirect, request, send_from_directory

from escalation.control_channel import ControlChannel

app = Flask(__name__)

PAGE = """<!doctype html>
<html><body style="font-family: sans-serif; max-width: 700px; margin: 2em auto;">
<h2>Operator Console (mock)</h2>
<p><b>Run:</b> {run_id}<br><b>Status:</b> {status}{signal_note}</p>
{body}
<hr>
<form method="post" action="/console/interrupt" onsubmit="return confirm('End this run outright?');">
  <input type="hidden" name="run_dir" value="{run_dir}">
  <button type="submit" style="background:#c0392b;color:white;">Interrupt Run</button>
  <span style="color:#666;"> — ends the run outright, whether or not anything is currently pending.</span>
</form>
</body></html>"""

PENDING = """
<table border="1" cellpadding="6" cellspacing="0">
  <tr><td>Reason</td><td>{reason}</td></tr>
  <tr><td>Step</td><td>{step_id}</td></tr>
  <tr><td>Capability</td><td>{capability}</td></tr>
  <tr><td>CDP endpoint</td><td>{cdp_endpoint}</td></tr>
</table>
<p><img src="/console/screenshot?run_dir={run_dir}" width="640"></p>
<p>To drive the live page directly: open <code>chrome://inspect</code> in a real Chrome,
add the CDP endpoint above under "Discover network targets", then click "inspect" on the
page listed there.</p>
<form method="post" action="/console/resume">
  <input type="hidden" name="run_dir" value="{run_dir}">
  <label>What did you do? (recorded on the run's evidence)</label><br>
  <textarea name="actions_taken" rows="3" cols="60"></textarea><br>
  <button type="submit">Resume Automation</button>
</form>
<form method="post" action="/console/cancel">
  <input type="hidden" name="run_dir" value="{run_dir}">
  <button type="submit">Cancel — give up on this one</button>
  <span style="color:#666;"> — stops waiting immediately; the run reports a failure instead of hanging.</span>
</form>
"""

IDLE = """
<p>No pending intervention for this run.</p>
<form method="post" action="/console/takeover">
  <input type="hidden" name="run_dir" value="{run_dir}">
  <button type="submit">Take Over Now</button>
  <span style="color:#666;"> — pause on whatever step runs next, even though it wasn't flagged risky.</span>
</form>
"""


@app.route("/console")
def console():
    run_dir = Path(request.args["run_dir"])
    control = ControlChannel(run_id=run_dir.name, evidence_dir=run_dir)
    state = control.status()
    req = state.get("intervention_request")
    body = IDLE.format(run_dir=str(run_dir))
    if req and state.get("status") in ("paused_for_human", "human_active"):
        body = PENDING.format(run_dir=str(run_dir), **req)
    signal = state.get("signal")
    signal_note = f" (pending signal: {signal['type']})" if signal else ""
    return PAGE.format(run_id=run_dir.name, status=state.get("status"), signal_note=signal_note,
                        body=body, run_dir=str(run_dir))


@app.route("/console/screenshot")
def screenshot():
    run_dir = Path(request.args["run_dir"])
    control = ControlChannel(run_id=run_dir.name, evidence_dir=run_dir)
    state = control.status()
    shot = (state.get("intervention_request") or {}).get("screenshot_path")
    if not shot:
        return "no screenshot on file", 404
    return send_from_directory(run_dir, shot)


@app.route("/console/resume", methods=["POST"])
def resume():
    run_dir = Path(request.form["run_dir"])
    control = ControlChannel(run_id=run_dir.name, evidence_dir=run_dir)
    control.mark_human_active()
    actions = request.form.get("actions_taken", "").strip()
    if actions:
        control.record_human_action(actions)
    control.signal_resume()
    return redirect(f"/console?run_dir={run_dir}")


@app.route("/console/takeover", methods=["POST"])
def takeover():
    """Tier 2 #7: request a voluntary pause on whatever step the run is
    about to execute next, even though nothing has been flagged risky.
    replay/executor.py polls for this at every step boundary."""
    run_dir = Path(request.form["run_dir"])
    control = ControlChannel(run_id=run_dir.name, evidence_dir=run_dir)
    control.request_takeover(reason="Operator requested manual takeover from the console.")
    return redirect(f"/console?run_dir={run_dir}")


@app.route("/console/cancel", methods=["POST"])
def cancel():
    """Tier 2 #7: give up on a pending, stuck escalation right now rather
    than waiting out a timeout. Only meaningful while an intervention is
    actually pending; the run reports a controlled FAILURE."""
    run_dir = Path(request.form["run_dir"])
    control = ControlChannel(run_id=run_dir.name, evidence_dir=run_dir)
    control.cancel(reason="Operator cancelled the pending intervention from the console.")
    return redirect(f"/console?run_dir={run_dir}")


@app.route("/console/interrupt", methods=["POST"])
def interrupt():
    """Tier 2 #7: end the run outright, at any point. Delivered to
    replay/executor.py as RunInterrupted (a BaseException — see
    escalation/transport.py) and reported as ReplayOutcome.INTERRUPTED."""
    run_dir = Path(request.form["run_dir"])
    control = ControlChannel(run_id=run_dir.name, evidence_dir=run_dir)
    control.interrupt(reason="Operator interrupted the run from the console.")
    return redirect(f"/console?run_dir={run_dir}")


if __name__ == "__main__":
    port = int(os.environ.get("OPERATOR_CONSOLE_PORT", 5056))
    app.run(host="127.0.0.1", port=port, debug=False)
