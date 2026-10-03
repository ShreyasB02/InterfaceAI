"""
The seam between "the recorded flow" and "how we perceive and act on a
surface".

The replay engine (replay/executor.py) speaks only this interface. It knows
steps, ranked locator specs, checkpoints, outcomes and handoffs; it does not
know what a browser is. A surface knows how to find the control a
`LocatorSpec` describes and how to act on it, and nothing about artifacts'
control flow.

One implementation exists: `PlaywrightSurface` (replay/playwright_surface.py)
for web apps. What a second one would have to provide is exactly this class:

  - a legacy web app with framesets: the same Playwright driver, with
    frame-aware `resolve()`;
  - a desktop app: `resolve()` over the OS accessibility tree (UIA / AX),
    where role + name locators carry over unchanged, `location()` returning
    the window/screen identity, `snapshot()` dumping the accessibility tree,
    and `attach_endpoint` being a VNC/RDP address instead of a CDP one.

tests/test_surface_seam.py runs a real artifact through the real executor on
an in-memory surface with no browser at all, which is what keeps this seam
honest.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from artifacts.schema import LocatorMethod, LocatorSpec


class SurfaceError(Exception):
    """An action on the surface failed or timed out: a navigation that
    didn't load, a control that stopped responding, a session that died.
    Carries the surface's own message; the executor turns it into a
    structured FAILURE."""


class TargetNotFound(Exception):
    """No strategy in a LocatorSpec resolved to exactly one control.
    `attempts` says what each strategy found, for the failure report."""

    def __init__(self, spec: LocatorSpec, attempts: list[str]):
        self.spec = spec
        self.attempts = attempts
        super().__init__(
            "No locator strategy resolved to exactly one element. Tried: " + "; ".join(attempts)
        )


@dataclass
class ResolvedTarget:
    """A control a surface found. `handle` is the surface's own reference
    to it and is opaque to the executor; `strategy_index` is which ranked
    strategy matched (0 = the primary), which is the drift signal."""
    handle: Any
    strategy_index: int
    method: LocatorMethod


def describe_strategy(strategy) -> str:
    """Human-readable form of one locator strategy, for failure messages."""
    if strategy.method == LocatorMethod.ROLE and strategy.role_name:
        return f"role={strategy.value!r} name={strategy.role_name!r}"
    return f"{strategy.method.value}={strategy.value!r}"


class Surface(ABC):
    """Everything the replay engine needs from the thing it is automating."""

    # -- session ---------------------------------------------------------

    @abstractmethod
    def open(self) -> None:
        """Start the session."""

    @abstractmethod
    def close(self) -> None:
        """End the session. Must be safe to call after a failure."""

    @abstractmethod
    def sign_in(self, base_url: str) -> None:
        """Establish an authenticated session. Infrastructure, not a
        recorded step: how a tenant logs in is the surface's business."""

    @property
    @abstractmethod
    def attach_endpoint(self) -> str:
        """Where a human operator attaches to THIS live session."""

    # -- location --------------------------------------------------------

    @abstractmethod
    def navigate(self, location: str) -> None: ...

    @abstractmethod
    def location(self) -> str:
        """Where the session currently is (a URL, for a web surface)."""

    @abstractmethod
    def reload(self) -> None: ...

    # -- controls --------------------------------------------------------

    @abstractmethod
    def resolve(self, spec: LocatorSpec, timeout_ms: int) -> ResolvedTarget:
        """First strategy, in rank order, matching exactly one visible
        control. An ambiguous match is a miss. Raises TargetNotFound."""

    @abstractmethod
    def is_present(self, spec: LocatorSpec, timeout_ms: int) -> bool:
        """Cheap probe: is something matching this spec on screen now?"""

    @abstractmethod
    def text_visible(self, text: str, timeout_ms: int) -> bool: ...

    @abstractmethod
    def click(self, target: ResolvedTarget) -> None: ...

    @abstractmethod
    def fill(self, target: ResolvedTarget, value: str) -> None: ...

    @abstractmethod
    def read_text(self, target: ResolvedTarget) -> str: ...

    @abstractmethod
    def expect_confirmation(self, answer: Optional[str]) -> None:
        """How to answer a native confirmation the next action may raise:
        'accept', 'dismiss', or None for the safe default (dismiss)."""

    # -- policy on the wire ----------------------------------------------

    @abstractmethod
    def begin_action(self, allow_risky: bool = False) -> None:
        """Mark the start of one action, and whether it has been cleared to
        make a risky, state-changing request."""

    @abstractmethod
    def check_action(self) -> None:
        """Raise AllowlistViolation / RiskyActionBlocked if the action just
        performed tried something policy forbids (it was already stopped)."""

    @abstractmethod
    def blocked_requests(self) -> list[str]: ...

    # -- evidence --------------------------------------------------------

    @abstractmethod
    def screenshot(self, path: str) -> bool:
        """Best effort; False if it couldn't be taken. Never raises."""

    @abstractmethod
    def snapshot(self) -> str:
        """A structural dump of the current screen (DOM, accessibility tree)."""

    # -- human control ---------------------------------------------------

    @abstractmethod
    def begin_human_control(self, control, reason: str) -> None:
        """A person now owns the session. Start observing what they do, and
        show them hand-back controls if the surface can."""

    @abstractmethod
    def end_human_control(self) -> list:
        """Automation owns the session again. Returns the ObservedActions."""

    @abstractmethod
    def idle(self, seconds: float) -> None:
        """Wait without acting, while still servicing the session."""
