"""
The web implementation of replay's Surface: Chromium driven by Playwright.
Everything Playwright-specific about replay lives here and in
replay/locator_resolver.py — the executor imports neither.

It composes the pieces that are web-specific by nature:
  - the browser, launched with a debugging port so an operator can attach
    to this exact session (`attach_endpoint` is its CDP address);
  - the request hook that enforces the allowlist on the wire
    (guardrails/network.py);
  - the in-page recorder and hand-back bar used while a human holds control
    (escalation/recorder.py, escalation/inpage.py);
  - native dialog handling.

Every Playwright failure leaves this module as a SurfaceError.
"""
from __future__ import annotations

from typing import Optional

from playwright.sync_api import Dialog, Error as PlaywrightError, sync_playwright

from artifacts.schema import LocatorSpec
from escalation.inpage import InPageHandBack
from escalation.recorder import HumanActionRecorder
from guardrails.allowlist import Allowlist
from guardrails.credentials import operator_credentials
from guardrails.network import NetworkGuard
from replay.locator_resolver import build_locator, resolve
from replay.surface import ResolvedTarget, Surface, SurfaceError


def _surface_errors(method):
    """Translate Playwright's errors at the boundary, keeping the first
    line of its message (the rest is a call log)."""
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except PlaywrightError as e:
            raise SurfaceError(f"{type(e).__name__}: {str(e).splitlines()[0]}") from e
    wrapper.__name__, wrapper.__doc__ = method.__name__, method.__doc__
    return wrapper


class PlaywrightSurface(Surface):
    def __init__(self, allowlist: Allowlist, cdp_port: int, headless: bool = True,
                 in_page_controls: Optional[bool] = None):
        self._allowlist = allowlist
        self._cdp_port = cdp_port
        self._headless = headless
        self._pw = self._browser = self._page = None
        self._guard = NetworkGuard(allowlist)
        self._recorder = HumanActionRecorder()
        # Hand-back buttons in the browser window itself. Default: only when
        # there is a window for a person to see (escalation/inpage.py).
        show_controls = (not headless) if in_page_controls is None else in_page_controls
        self._inpage = InPageHandBack() if show_controls else None
        self._confirmation_answer: Optional[str] = None
        self._human_in_control = False

    # -- session ---------------------------------------------------------

    @_surface_errors
    def open(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless, args=[f"--remote-debugging-port={self._cdp_port}"])
        self._page = self._browser.new_page()
        self._page.on("dialog", self._on_dialog)
        self._recorder.install(self._page)
        if self._inpage:
            self._inpage.install(self._page)
        self._guard.install(self._page.context)

    def close(self) -> None:
        for closer in (lambda: self._browser and self._browser.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001 - already failing or already closed
                pass

    @property
    def attach_endpoint(self) -> str:
        return f"http://127.0.0.1:{self._cdp_port}"

    @_surface_errors
    def sign_in(self, base_url: str) -> None:
        employee_id, passcode = operator_credentials()
        login_url = base_url.rstrip("/") + "/login"
        self._allowlist.check_url(login_url)
        self._guard.begin()
        self._page.goto(login_url)
        self._page.locator('input[name="employee_id"]').fill(employee_id)
        self._page.locator('input[name="passcode"]').fill(passcode)
        self._page.get_by_role("button", name="Log In").click()
        self._guard.raise_if_blocked()
        self._allowlist.check_url(self._page.url)

    # -- location --------------------------------------------------------

    @_surface_errors
    def navigate(self, location: str) -> None:
        self._page.goto(location)

    def location(self) -> str:
        try:
            return self._page.url
        except Exception:  # noqa: BLE001
            return "<unavailable>"

    @_surface_errors
    def reload(self) -> None:
        self._page.reload()

    # -- controls --------------------------------------------------------

    @_surface_errors
    def resolve(self, spec: LocatorSpec, timeout_ms: int) -> ResolvedTarget:
        found = resolve(self._page, spec, timeout_ms=timeout_ms)
        return ResolvedTarget(handle=found.locator, strategy_index=found.strategy_index, method=found.method)

    def is_present(self, spec: LocatorSpec, timeout_ms: int) -> bool:
        for strategy in spec.strategies:
            try:
                locator = build_locator(self._page, strategy)
                locator.first.wait_for(state="visible", timeout=timeout_ms)
                if locator.count() >= 1:
                    return True
            except Exception:  # noqa: BLE001 - a miss on a probe is not an error
                continue
        return False

    def text_visible(self, text: str, timeout_ms: int) -> bool:
        try:
            self._page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001
            return False

    @_surface_errors
    def click(self, target: ResolvedTarget) -> None:
        target.handle.click()

    @_surface_errors
    def fill(self, target: ResolvedTarget, value: str) -> None:
        target.handle.fill(value)

    @_surface_errors
    def read_text(self, target: ResolvedTarget) -> str:
        return target.handle.inner_text().strip()

    def expect_confirmation(self, answer: Optional[str]) -> None:
        self._confirmation_answer = answer

    def _on_dialog(self, dialog: Dialog) -> None:
        if self._human_in_control:
            # A human (or the scripted operator, via a separate CDP
            # connection) holds the session and has their own dialog
            # listener. Don't race them — just note that it appeared, as
            # part of what happened on their watch.
            self._recorder.note_dialog(dialog.message)
            return
        try:
            (dialog.accept if self._confirmation_answer == "accept" else dialog.dismiss)()
        except Exception:  # noqa: BLE001
            # Benign race: the operator's connection resolved this same
            # dialog just before our listener was scheduled.
            pass

    # -- policy on the wire ----------------------------------------------

    def begin_action(self, allow_risky: bool = False) -> None:
        self._guard.begin(allow_risky=allow_risky)

    def check_action(self) -> None:
        self._guard.raise_if_blocked()

    def blocked_requests(self) -> list[str]:
        return list(self._guard.violations)

    # -- evidence --------------------------------------------------------

    def screenshot(self, path: str) -> bool:
        try:
            self._page.screenshot(path=path)
            return True
        except Exception:  # noqa: BLE001 - a screenshot must never take a run down
            return False

    @_surface_errors
    def snapshot(self) -> str:
        return self._page.content()

    # -- human control ---------------------------------------------------

    def begin_human_control(self, control, reason: str) -> None:
        self._human_in_control = True
        # The human may perform risky actions — that is what they are here
        # for — but the allowlist still binds the session they are driving.
        self._guard.begin(allow_risky=True)
        self._recorder.start()
        if self._inpage:
            self._inpage.show(control, reason)

    def end_human_control(self) -> list:
        observed = self._recorder.stop()
        if self._inpage:
            self._inpage.hide()
        self._human_in_control = False
        return observed

    @_surface_errors
    def idle(self, seconds: float) -> None:
        # Not time.sleep(): the recorder's and the request hook's callbacks
        # are only delivered while this thread is inside a Playwright call.
        self._page.wait_for_timeout(seconds * 1000)
