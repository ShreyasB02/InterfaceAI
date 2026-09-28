"""Defensive wrappers around external calls that must never themselves
crash a run. Screenshot capture is the concrete case here: it happens on
almost every observation and, critically, on both sides of a human
escalation — the single most safety-critical path in the system, since
that's exactly the moment a human is being brought in to look at
something that's already gone wrong. A screenshot failure there (page
mid-navigation, browser closing, a transient Playwright error) must not
also take down the mechanism that was about to notify a human.
"""
from __future__ import annotations


def safe_screenshot(page, path: str) -> bool:
    """Best-effort screenshot; returns True on success, False on any
    failure. Callers treat False as "no screenshot this time" — never as a
    reason to raise or abort whatever they were doing."""
    try:
        page.screenshot(path=path)
        return True
    except Exception:
        return False
