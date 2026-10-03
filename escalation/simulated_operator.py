"""
Proves the handoff mechanism is real rather than mocked: this reattaches to
the SAME live browser process the run was driving, via Chrome DevTools
Protocol, using a completely separate Playwright connection — exactly the
seam a real operator's browser would use. Only the *decisions* (which
controls to use) are scripted here, for repeatable evidence; the
reattachment, the control transfer, and the recording of what was done
(escalation/recorder.py, on the run's side) are not.

Must run in its own thread (Playwright's sync API cannot have two
`sync_playwright()` instances live in the same thread).
"""
from __future__ import annotations

import time
from typing import Optional, Sequence

from playwright.sync_api import sync_playwright

from escalation.transport import ControlTransport

# ("click", "<button or link accessible name>") | ("fill", "<field name attribute>", "<value>")
OperatorAction = tuple


def parse_action(spec: str) -> OperatorAction:
    """CLI form: 'click:Confirm & Open Account' or 'fill:nickname=Vacation Fund'."""
    kind, _, rest = spec.partition(":")
    if kind == "click" and rest:
        return ("click", rest)
    if kind == "fill" and "=" in rest:
        name, _, value = rest.partition("=")
        return ("fill", name, value)
    raise ValueError(f"Operator action must be 'click:<label>' or 'fill:<field>=<value>', got {spec!r}")


def simulate_operator_takeover(control: ControlTransport, click_role_name: Optional[str] = None,
                                accept_dialog: bool = True, reaction_delay_s: float = 1.0,
                                actions: Optional[Sequence[OperatorAction]] = None,
                                step_done: Optional[bool] = None, action_gap_s: float = 0.6) -> None:
    script = list(actions or [])
    if click_role_name:
        script.append(("click", click_role_name))

    req = control.status().get("intervention_request")
    if not req:
        raise RuntimeError("No pending intervention_request on the control channel to attach to.")

    time.sleep(reaction_delay_s)  # models the time a real operator takes to look and react
    control.mark_human_active()

    with sync_playwright() as p:
        remote_browser = p.chromium.connect_over_cdp(req["cdp_endpoint"])
        page = remote_browser.contexts[0].pages[0]  # the SAME page the run was on — not a fresh one

        def _on_dialog(dialog):
            try:
                dialog.accept() if accept_dialog else dialog.dismiss()
            except Exception:
                pass  # benign race with the run's own listener on the same browser-side dialog

        page.on("dialog", _on_dialog)
        for action in script:
            if action[0] == "fill":
                page.locator(f'[name="{action[1]}"]').fill(action[2])
            else:
                target = page.get_by_role("button", name=action[1], exact=True)
                if target.count() == 0:
                    target = page.get_by_role("link", name=action[1], exact=True)
                target.click()
            time.sleep(action_gap_s)  # human-scale pacing; also lets a dialog/navigation settle
        # Deliberately not calling remote_browser.close() — this is a CDP
        # client connection, not the browser's owner; the run's own
        # connection is still using this same browser process.

    control.record_human_action(
        f"Scripted operator performed {len(script)} action(s) and handed control back.")
    control.signal_resume(step_done=step_done)
