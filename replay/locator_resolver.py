"""
Resolves an artifact's ranked LocatorSpec against the live page. Tries
each strategy in order; the first one that resolves to exactly one
visible element wins. An ambiguous match (more than one element) is
treated the same as a miss and falls through to the next strategy —
silently acting on "one of N" matches is exactly the kind of blind
proceeding the brief warns against.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Locator, Page

from artifacts.schema import LocatorMethod, LocatorSpec


class LocatorResolutionError(Exception):
    def __init__(self, spec: LocatorSpec, attempts: list[str]):
        self.spec = spec
        self.attempts = attempts
        super().__init__(
            "No locator strategy resolved to exactly one element. Tried: " + "; ".join(attempts)
        )


@dataclass
class ResolvedLocator:
    locator: Locator
    strategy_index: int
    method: LocatorMethod


def describe(strategy) -> str:
    """Human-readable form of one strategy, for failure messages."""
    if strategy.method == LocatorMethod.ROLE and strategy.role_name:
        return f"role={strategy.value!r} name={strategy.role_name!r}"
    return f"{strategy.method.value}={strategy.value!r}"


def build_locator(page: Page, strategy) -> Locator:
    if strategy.method == LocatorMethod.CSS:
        return page.locator(strategy.value)
    if strategy.method == LocatorMethod.ROLE:
        if strategy.role_name:
            return page.get_by_role(strategy.value, name=strategy.role_name, exact=True)
        return page.get_by_role(strategy.value)
    if strategy.method == LocatorMethod.TEXT:
        return page.get_by_text(strategy.value, exact=False)
    if strategy.method == LocatorMethod.XPATH:
        return page.locator(f"xpath={strategy.value}")
    raise ValueError(f"Unknown locator method: {strategy.method}")


def resolve(page: Page, spec: LocatorSpec, timeout_ms: int = 3000) -> ResolvedLocator:
    attempts: list[str] = []
    for i, strategy in enumerate(spec.strategies):
        try:
            loc = build_locator(page, strategy)
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            count = loc.count()
        except Exception as e:  # noqa: BLE001 - a failed wait/count is a miss, not a crash
            attempts.append(f"[{i}] {describe(strategy)} -> not found: {str(e).splitlines()[0]}")
            continue
        if count == 1:
            return ResolvedLocator(locator=loc, strategy_index=i, method=strategy.method)
        attempts.append(f"[{i}] {describe(strategy)} -> ambiguous: matched {count} elements")
    raise LocatorResolutionError(spec, attempts)
