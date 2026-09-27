"""
Proves the handoff mechanism is real rather than mocked: this reattaches to
the SAME live browser process replay was driving, via Chrome DevTools
Protocol, using a completely separate Playwright connection — exactly the
seam a real operator's browser would use. Only the *decision* of what to
click is scripted here, for repeatable evidence; the reattachment and
control transfer are not.

Must run in its own thread (Playwright's sync API cannot have two
`sync_playwright()` instances live in the same thread — verified while
building this).
"""
from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

from escalation.control_channel import ControlChannel


def simulate_operator_takeover(control: ControlChannel, click_role_name: str,
                                accept_dialog: bool = True, reaction_delay_s: float = 1.0) -> None:
    state = control.status()
    req = state.get("intervention_request")
    if not req:
        raise RuntimeError("No pending intervention_request on the control channel to attach to.")
    cdp_endpoint = req["cdp_endpoint"]

    time.sleep(reaction_delay_s)  # models the time a real operator takes to look and react
    control.mark_human_active()

    with sync_playwright() as p:
        remote_browser = p.chromium.connect_over_cdp(cdp_endpoint)
        context = remote_browser.contexts[0]
        page = context.pages[0]  # the SAME page replay was on — not a fresh one

        dialog_seen = {}

        def _on_dialog(dialog):
            dialog_seen["message"] = dialog.message
            try:
                dialog.accept() if accept_dialog else dialog.dismiss()
            except Exception:
                pass  # benign race with replay's own listener on the same browser-side dialog

        page.on("dialog", _on_dialog)
        page.get_by_role("button", name=click_role_name).click()
        time.sleep(0.3)  # let the dialog/navigation settle before disconnecting

        action_desc = f"Clicked '{click_role_name}'"
        if dialog_seen:
            action_desc += f"; {'accepted' if accept_dialog else 'dismissed'} dialog ({dialog_seen['message']!r})"
        control.record_human_action(action_desc)
        # Deliberately not calling remote_browser.close() — this is a CDP
        # client connection, not the browser's owner; replay's own
        # connection is still using this same browser process.

    control.signal_resume()
