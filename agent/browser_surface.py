"""
The browser surface: the concrete "how we perceive/act on a surface" layer
that both the discovery agent and (indirectly, via the same locator
inference logic) the replay engine's understanding of the app are built on.

Two things are deliberately kept separate here, because they answer
different questions:

  - The *action selector* (a transient `data-cua-idx` attribute this class
    injects into the live DOM on every observe()) is how discovery reliably
    clicks/fills element N *right now*, in this one session. It is never
    written into a recorded Step or persisted anywhere.
  - The *candidate locator strategies* (derived in `locator_inference.py`
    from each element's real attributes — name, role, text, label-relative
    position) are what actually gets recorded into the artifact for replay
    to use later, against a fresh page load that has no injected attributes
    at all. Replay never depends on anything discovery-only.

This separation is also the seam Section 3.7 asks about: swapping this
class for an accessibility-tree-only or OS-automation backend (for a
desktop surface) only has to preserve the `observe()`/`act()` contract —
the artifact schema and everything above this file doesn't change.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from playwright.sync_api import Dialog, Page, sync_playwright

from escalation.inpage import InPageHandBack
from escalation.recorder import HumanActionRecorder
from guardrails.allowlist import Allowlist
from guardrails.network import NetworkGuard
from guardrails.safety import safe_screenshot
from agent.locator_inference import ElementMeta, derive_locator_strategies

INTERACTIVE_ELEMENTS_JS = """
() => {
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 &&
           style.visibility !== 'hidden' && style.display !== 'none';
  };
  const nearbyLabel = (el) => {
    const td = el.closest('td');
    if (td && td.previousElementSibling && td.previousElementSibling.tagName === 'TD') {
      return td.previousElementSibling.innerText.trim();
    }
    return null;
  };
  const nodes = document.querySelectorAll('input, button, select, textarea, a[href]');
  const out = [];
  let idx = 0;
  nodes.forEach((el) => {
    if (!isVisible(el)) return;
    el.setAttribute('data-cua-idx', String(idx));
    out.push({
      index: idx,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      value: el.tagName.toLowerCase() === 'input' ? (el.value || '') : '',
      text: (el.innerText || el.textContent || '').trim().slice(0, 80),
      label: nearbyLabel(el),
    });
    idx += 1;
  });
  return out;
}
"""


@dataclass
class Observation:
    url: str
    title: str
    page_text: str
    elements: list[ElementMeta]
    screenshot_path: str


@dataclass
class ActionRecord:
    """One executed action, in enough detail to become an artifact Step."""
    kind: str  # navigate | click | fill | wait_for_text | extract
    intent: str
    target_element: Optional[ElementMeta] = None
    value: Optional[str] = None
    output_name: Optional[str] = None
    extracted_value: Optional[str] = None
    extracted_xpath: Optional[str] = None
    dialog_message: Optional[str] = None
    dialog_action: Optional[str] = None
    nav_path: Optional[str] = None
    # The action made a state-changing request to a route policy marks risky.
    risky_request: bool = False


class BrowserSurface:
    def __init__(self, base_url: str, evidence_dir: Path, allowlist: Allowlist, headless: bool = True,
                 cdp_port: int = 0, in_page_controls: Optional[bool] = None):
        self.base_url = base_url.rstrip("/")
        self.evidence_dir = evidence_dir
        self.allowlist = allowlist
        self.headless = headless
        self._pw = None
        self._browser = None
        self.page: Optional[Page] = None
        self._shot_count = 0
        self._pending_dialog: Optional[Dialog] = None
        self._pending_dialog_action: Optional[str] = None
        # The session is launched with a CDP port open from the start, so a
        # human operator can attach to THIS browser if the run escalates.
        self.cdp_endpoint = f"http://127.0.0.1:{cdp_port}"
        self._cdp_port = cdp_port
        self.recorder = HumanActionRecorder()
        self.guard = NetworkGuard(allowlist)
        show_controls = (not headless) if in_page_controls is None else in_page_controls
        self.inpage = InPageHandBack() if show_controls else None

    def __enter__(self) -> "BrowserSurface":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless, args=[f"--remote-debugging-port={self._cdp_port}"])
        self.page = self._browser.new_page()
        self.page.on("dialog", self._on_dialog)
        self.recorder.install(self.page)
        if self.inpage:
            self.inpage.install(self.page)
        self.guard.install(self.page.context)
        (self.evidence_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def _on_dialog(self, dialog: Dialog) -> None:
        # Sync API: this callback runs while the triggering action (click)
        # is still blocked waiting for the dialog to be resolved. If the
        # action that opened it told us what to do, do it; otherwise the
        # safe default is dismiss (never silently accept an unplanned
        # irreversible confirmation).
        if self.recorder.active:
            # A human holds the session and their own connection resolves
            # the dialog. Note that it appeared; don't race them for it.
            self.recorder.note_dialog(dialog.message)
            return
        self._pending_dialog = dialog
        action = self._pending_dialog_action or "dismiss"
        self._pending_dialog_action = action
        if action == "accept":
            dialog.accept()
        else:
            dialog.dismiss()

    def _resolve_url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        return urljoin(self.base_url + "/", path_or_url.lstrip("/"))

    def screenshot(self, tag: str) -> str:
        return self._screenshot(tag)

    def _screenshot(self, tag: str) -> str:
        self._shot_count += 1
        name = f"{self._shot_count:03d}_{tag}.png"
        path = self.evidence_dir / "screenshots" / name
        safe_screenshot(self.page, str(path))
        return f"screenshots/{name}"

    # -- perception -----------------------------------------------------

    def observe(self, tag: str = "observe") -> Observation:
        raw_elements = self.page.evaluate(INTERACTIVE_ELEMENTS_JS)
        elements = [ElementMeta(**e) for e in raw_elements]
        body_text = self.page.evaluate("() => document.body.innerText") or ""
        shot = self._screenshot(tag)
        return Observation(
            url=self.page.url,
            title=self.page.title(),
            page_text=body_text.strip()[:4000],
            elements=elements,
            screenshot_path=shot,
        )

    # -- action -----------------------------------------------------------

    def navigate(self, path: str) -> ActionRecord:
        url = self._resolve_url(path)
        self.allowlist.check_action("navigate")
        self.allowlist.check_url(url)
        self.guard.begin()
        self.page.goto(url)
        self.guard.raise_if_blocked()
        self.allowlist.check_url(self.page.url)
        return ActionRecord(kind="navigate", intent=f"Navigate to {path}", nav_path=path)

    def click(self, index: int, elements: list[ElementMeta], on_dialog: Optional[str] = None,
              allow_risky: bool = False) -> ActionRecord:
        """`allow_risky` clears this one click to make a state-changing
        request to a risky route. Without it such a request is aborted on
        the wire (guardrails/network.py) and RiskyActionBlocked is raised."""
        self.allowlist.check_action("click")
        el = next(e for e in elements if e.index == index)
        self._pending_dialog = None
        self._pending_dialog_action = on_dialog
        locator = self.page.locator(f'[data-cua-idx="{index}"]')
        self.guard.begin(allow_risky=allow_risky)
        locator.click()
        self.guard.raise_if_blocked()
        self.allowlist.check_url(self.page.url)
        rec = ActionRecord(
            kind="click",
            intent=f"Click {el.describe()}",
            target_element=el,
        )
        rec.risky_request = bool(self.guard.risky_allowed)
        if self._pending_dialog is not None:
            rec.dialog_message = self._pending_dialog.message
            rec.dialog_action = self._pending_dialog_action
        return rec

    def fill(self, index: int, value: str, elements: list[ElementMeta]) -> ActionRecord:
        self.allowlist.check_action("fill")
        el = next(e for e in elements if e.index == index)
        locator = self.page.locator(f'[data-cua-idx="{index}"]')
        locator.fill(value)
        return ActionRecord(
            kind="fill",
            intent=f"Fill {el.describe()} with a supplied value",
            target_element=el,
            value=value,
        )

    def wait_for_text(self, text: str, timeout_ms: int = 8000) -> ActionRecord:
        self.allowlist.check_action("wait_for")
        self.page.get_by_text(text, exact=False).first.wait_for(timeout=timeout_ms)
        return ActionRecord(kind="wait_for_text", intent=f"Wait for text '{text}' to appear", value=text)

    def extract_field(self, label: str, output_name: str, cell_index: int = -1) -> ActionRecord:
        self.allowlist.check_action("extract")
        row = self.page.locator(f"xpath=//tr[td[normalize-space()='{label}']]").first
        cells = row.locator("td")
        count = cells.count()
        idx = cell_index if cell_index >= 0 else count + cell_index
        text = cells.nth(idx).inner_text().strip()
        xpath = f"//tr[td[normalize-space()='{label}']]/td[{idx + 1}]"
        return ActionRecord(
            kind="extract",
            intent=f"Read the '{label}' value into {output_name}",
            output_name=output_name,
            extracted_value=text,
            extracted_xpath=xpath,
        )
