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
<p><b>Run:</b> {run_id}<br><b>Status:</b> {status}</p>
{body}
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
"""

IDLE = "<p>No pending intervention for this run.</p>"


@app.route("/console")
def console():
    run_dir = Path(request.args["run_dir"])
    control = ControlChannel(run_id=run_dir.name, evidence_dir=run_dir)
    state = control.status()
    req = state.get("intervention_request")
    body = IDLE
    if req and state.get("status") in ("paused_for_human", "human_active"):
        body = PENDING.format(run_dir=str(run_dir), **req)
    return PAGE.format(run_id=run_dir.name, status=state.get("status"), body=body)


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


if __name__ == "__main__":
    port = int(os.environ.get("OPERATOR_CONSOLE_PORT", 5056))
    app.run(host="127.0.0.1", port=port, debug=False)
