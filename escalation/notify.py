"""
Tells whoever is at the terminal that a run has stopped for a human, and
exactly what to do about it. The run itself blocks silently while it waits;
without this the CLI just looks hung.

Used by both CLIs as (part of) their on_escalation hook. A real deployment
would route the same request to a queue or a pager instead of stdout.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote


def console_url(run_dir: str) -> str:
    port = os.environ.get("OPERATOR_CONSOLE_PORT", "5056")
    return f"http://127.0.0.1:{port}/console?run_dir={quote(str(Path(run_dir).resolve()))}"


def announce_pause(control, ctx: dict, headed: bool) -> None:
    req = control.status().get("intervention_request") or {}
    where = ("the browser window this run opened" if headed
             else f"chrome://inspect -> Configure -> add {req.get('cdp_endpoint', '').replace('http://', '')} -> inspect")
    print(
        "\n" + "=" * 72 + "\n"
        f"PAUSED — waiting for a human ({req.get('kind', 'intervention')})\n"
        f"  Why:      {req.get('reason')}\n"
        f"  Act in:   {where}\n"
        + ("  Then:     use the bar at the bottom of that window to hand back\n"
           f"            (or the console: {console_url(ctx['run_dir'])})\n" if headed else
           f"  Then:     hand back at {console_url(ctx['run_dir'])}\n"
           "            (start the console first: python -m escalation.operator_console)\n")
        + "=" * 72,
        file=sys.stderr, flush=True,
    )
