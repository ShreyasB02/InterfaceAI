"""
Hand-back controls inside the live browser window, so an operator who is
already acting on the page doesn't have to switch to the console to return
control.

While a human holds the session, a bar is drawn at the bottom of the page
saying why the run stopped, with the same three decisions the console
offers: "I completed this step", "automation should run this step", and
"cancel". A click goes through a Playwright binding to this process, which
writes the decision to the same ControlTransport the console writes to —
it is a second front end, not a second mechanism.

Only used for headed runs. In a headless run nobody can see it, and a fixed
bar could sit on top of a control a scripted operator needs to click.

The page being automated is not trusted to press these buttons itself (a
compromised or merely odd app script approving its own irreversible step
would defeat the point of asking a human):

  - the handler ignores events that aren't `isTrusted`, so script-made
    clicks (`element.click()`, `dispatchEvent`) do nothing;
  - the binding only acts on a per-intervention nonce that lives in the
    injected script's closure, which page scripts cannot read, so calling
    the binding directly does nothing either.

The out-of-band console remains the stronger channel, and the only one for
a headless or remote session.
"""
from __future__ import annotations

import json
import secrets
from typing import Optional

from escalation.transport import ControlTransport

BINDING_NAME = "__cuaHandBack"
STATE_BINDING_NAME = "__cuaHandBackState"
BAR_ID = "__cua_handoff_bar"

# Runs in every document. Asks this process whether a human currently holds
# the session and, if so, draws the bar. Idempotent.
BAR_JS = """
(() => {
  const BAR_ID = %(bar_id)s;
  const draw = (state) => {
    const existing = document.getElementById(BAR_ID);
    if (existing) existing.remove();
    if (!state || !document.documentElement) return;
    const nonce = state.nonce;  // closure-only: never placed in the DOM
    const host = document.createElement('div');
    host.id = BAR_ID;
    host.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;';
    const root = host.attachShadow({mode: 'open'});
    const bar = document.createElement('div');
    bar.style.cssText = 'font:14px/1.4 -apple-system,Segoe UI,sans-serif;background:#1f2a44;color:#fff;' +
                        'padding:10px 14px;display:flex;gap:10px;align-items:center;' +
                        'box-shadow:0 -2px 8px rgba(0,0,0,.35);';
    const text = document.createElement('div');
    text.style.cssText = 'flex:1;';
    const title = document.createElement('b');
    title.textContent = 'Automation paused — you have control. ';
    const why = document.createElement('span');
    why.textContent = state.reason;
    text.append(title, why);
    bar.append(text);
    const button = (label, decision, background) => {
      const b = document.createElement('button');
      b.textContent = label;
      b.dataset.decision = decision;
      b.style.cssText = 'font:inherit;padding:6px 10px;border:0;border-radius:4px;cursor:pointer;' +
                        'color:#fff;background:' + background + ';';
      b.addEventListener('click', (e) => {
        if (!e.isTrusted) return;            // only a real input event counts
        e.stopPropagation();
        bar.style.opacity = '0.6';
        window[%(binding)s]({nonce, decision});
      });
      return b;
    };
    bar.append(button('I completed this step — continue', 'done', '#2e7d32'),
               button('Hand back — automation runs this step', 'retry', '#1565c0'),
               button('Cancel', 'cancel', '#b23b3b'));
    root.append(bar);
    document.documentElement.append(host);
  };
  window.__cuaDrawHandBack = draw;
  const ask = () => window[%(state_binding)s] && window[%(state_binding)s]().then(draw, () => {});
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ask);
  else ask();
})()
""" % {"bar_id": json.dumps(BAR_ID), "binding": json.dumps(BINDING_NAME),
       "state_binding": json.dumps(STATE_BINDING_NAME)}


class InPageHandBack:
    def __init__(self):
        self._page = None
        self._control: Optional[ControlTransport] = None
        self._state: Optional[dict] = None  # {"reason": ..., "nonce": ...} while a human holds control

    def install(self, page) -> None:
        """Call once, right after the page is created and before it navigates."""
        self._page = page
        page.context.expose_binding(BINDING_NAME, self._on_decision)
        page.context.expose_binding(STATE_BINDING_NAME, lambda source: self._state)
        page.context.add_init_script(BAR_JS)

    def show(self, control: ControlTransport, reason: str) -> None:
        self._control = control
        self._state = {"reason": reason, "nonce": secrets.token_urlsafe(16)}
        self._redraw()

    def hide(self) -> None:
        self._state = None
        self._control = None
        self._redraw()

    def _redraw(self) -> None:
        try:
            self._page.evaluate("(state) => window.__cuaDrawHandBack && window.__cuaDrawHandBack(state)",
                                self._state)
        except Exception:  # noqa: BLE001 - mid-navigation; the next document draws itself via the init script
            pass

    def _on_decision(self, source, payload) -> None:
        state, control = self._state, self._control
        if not state or control is None or not isinstance(payload, dict):
            return
        if payload.get("nonce") != state["nonce"]:
            return  # not from the bar this process drew
        decision = payload.get("decision")
        if decision == "cancel":
            control.cancel(reason="Operator cancelled from the in-page controls.")
            return
        if decision in ("done", "retry"):
            control.mark_human_active()
            control.record_human_action("Handed back from the in-page controls.")
            control.signal_resume(step_done=decision == "done")
