"""
Records what a human operator actually does on the live session while they
hold control — observed from the page itself, not self-reported.

How: a small script is injected into every document of the session's browser
context. It listens (capture phase) for the clicks and field changes a person
makes and reports each one to this process through a Playwright binding.
Main-frame navigations and native dialogs are picked up from Playwright's own
page events. Because the operator drives the SAME browser (over CDP, or the
headed window), their input produces the same DOM events any user's would.

The listeners fire for automation's own actions too, so recording is gated
on the Python side: nothing is kept unless `start()` has been called, which
callers do only between ceding control and taking it back.

Two consumers:
  - replay: the observed actions go into the run's evidence and its result.
  - discovery: they are also turned into artifact steps (the element
    metadata here is the same shape discovery's own observations use), so a
    flow that needed a human still records completely.

The raw typed value is kept in memory only, so discovery can bind it to an
input param. `describe()` is what gets written anywhere, and it never
contains a raw value.

The sync Playwright API only delivers binding callbacks while the owning
thread is inside a Playwright call, so whoever waits for the operator must
idle with `page.wait_for_timeout()`, not `time.sleep()` — see `pump()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

BINDING_NAME = "__cuaHumanAction"

RECORDER_JS = """
(() => {
  if (window.__cuaRecorderInstalled) return;
  window.__cuaRecorderInstalled = true;
  const labelFor = (el) => {
    const td = el.closest('td');
    if (td && td.previousElementSibling && td.previousElementSibling.tagName === 'TD') {
      return td.previousElementSibling.innerText.trim();
    }
    if (el.labels && el.labels.length) return el.labels[0].innerText.trim();
    return el.getAttribute('aria-label');
  };
  const meta = (el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type') || '',
    name: el.getAttribute('name') || '',
    text: (el.tagName === 'INPUT' ? '' : (el.innerText || el.textContent || '')).trim().slice(0, 80),
    button_value: el.tagName === 'INPUT' ? (el.value || '') : '',
    label: labelFor(el),
  });
  const report = (payload) => { try { window.%(binding)s(payload); } catch (e) {} };
  const CLICKABLE = 'a[href], button, input[type=submit], input[type=button], input[type=checkbox], input[type=radio]';
  document.addEventListener('click', (e) => {
    const el = e.target.closest && e.target.closest(CLICKABLE);
    if (el) report({kind: 'click', ...meta(el)});
  }, true);
  // Typing fires `input` immediately; `change` only fires when the field
  // loses focus. `input` is reported so this side always knows the value a
  // field currently holds and who put it there (see _automation_values).
  document.addEventListener('input', (e) => {
    const el = e.target;
    if (el.matches && el.matches('input, select, textarea')) {
      report({kind: 'input', ...meta(el), button_value: '', value: el.value});
    }
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el.matches && el.matches('input, select, textarea')) {
      report({kind: 'fill', ...meta(el), button_value: '', value: el.value});
    }
  }, true);
})()
""" % {"binding": BINDING_NAME}


@dataclass
class ObservedAction:
    kind: str  # click | fill | navigate | dialog
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tag: str = ""
    type: str = ""
    name: str = ""
    text: str = ""
    label: Optional[str] = None
    url: Optional[str] = None
    message: Optional[str] = None
    # Raw typed value. In-memory only: never serialized by describe()/to_evidence().
    value: Optional[str] = field(default=None, repr=False)

    def _target(self) -> str:
        if self.text:
            return f"'{self.text}' {self.tag}"
        if self.label:
            return f"the '{self.label}' field"
        return f"field '{self.name}'" if self.name else self.tag

    def describe(self, params: Optional[dict] = None) -> str:
        if self.kind == "click":
            return f"Clicked {self._target()}"
        if self.kind == "fill":
            param = next((k for k, v in (params or {}).items() if str(v) == (self.value or "")), None)
            # Either it is one of the run's declared inputs, named as such,
            # or it is something only the human knows — and then it is not
            # written down at all, sensitive-looking or not.
            shown = f"the supplied {param}" if param else "a value they entered (not recorded)"
            return f"Set {self._target()} to {shown}"
        if self.kind == "navigate":
            return f"Page navigated to {self.url}"
        return f"A dialog appeared: {self.message!r}"

    def to_evidence(self, params: Optional[dict] = None) -> dict:
        """The persisted form: what was done and to which control, no raw value."""
        out = {"at": self.at, "source": "observed", "kind": self.kind, "description": self.describe(params)}
        for key in ("tag", "name", "label", "text", "url"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out


class HumanActionRecorder:
    def __init__(self):
        self._active = False
        self._actions: list[ObservedAction] = []
        self._page = None
        # Field values as automation left them. A field automation filled
        # only fires its native `change` when it later loses focus —
        # possibly on the human's first click — and that must not be
        # credited to them. Nor is a human re-entering the value automation
        # already put there a change worth recording.
        self._automation_values: dict[str, str] = {}

    def install(self, page) -> None:
        """Call once, right after the page is created and before it navigates."""
        self._page = page
        page.context.expose_binding(BINDING_NAME, self._on_report)
        page.context.add_init_script(RECORDER_JS)
        page.on("framenavigated", self._on_navigated)

    def start(self) -> None:
        self._actions = []
        self._active = True

    def stop(self) -> list[ObservedAction]:
        self._active = False
        return self._actions

    @property
    def active(self) -> bool:
        return self._active

    def note_dialog(self, message: str) -> None:
        if self._active:
            self._actions.append(ObservedAction(kind="dialog", message=message))

    def pump(self, seconds: float) -> None:
        """Idle for `seconds` while still receiving the operator's actions."""
        self._page.wait_for_timeout(seconds * 1000)

    def _on_report(self, source, payload: dict) -> None:
        if not isinstance(payload, dict) or payload.get("kind") not in ("click", "fill", "input"):
            return
        kind = payload["kind"]
        if kind in ("fill", "input"):
            field_key = payload.get("name") or payload.get("label") or ""
            if not self._active:
                self._automation_values[field_key] = payload.get("value")
                return
            if kind == "input":
                return  # the human is mid-typing; the `change` that follows is the record
            if self._automation_values.get(field_key) == payload.get("value"):
                return
        if not self._active:
            return
        self._actions.append(ObservedAction(
            kind=kind, tag=payload.get("tag") or "", type=payload.get("type") or "",
            name=payload.get("name") or "",
            text=payload.get("text") or payload.get("button_value") or "",
            label=payload.get("label"), value=payload.get("value") if kind == "fill" else None,
        ))

    def _on_navigated(self, frame) -> None:
        if self._active and self._page is not None and frame == self._page.main_frame:
            self._actions.append(ObservedAction(kind="navigate", url=frame.url))
