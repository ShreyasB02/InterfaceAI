"""
The allowlist policy: where the agent may go and what it may do. One object,
built from config, used identically by discovery and replay — there is no
code path that enforces it on one side and not the other.

Four independent axes, all configurable (see .env.example):

  domains       hosts the session may talk to. Required. Empty means nothing
                is allowed: the policy fails closed, never open.
  routes        path globs permitted on those hosts (e.g. /members/*). Unset
                means every path on an allowed host.
  actions       step/tool action types that may be used at all (navigate,
                click, fill, ...). Unset means all. A read-only deployment
                would allow just navigate, click, wait_for, extract.
  risky_routes  path globs where a state-changing request (anything but GET)
                is treated as risky/irreversible — see guardrails/network.py
                and guardrails/risk_policy.py for what that triggers.

Enforcement happens at three points, earliest first:
  1. before an action: its type (`check_action`) and, for a navigation, its
     destination (`check_url`);
  2. as the browser issues each request (guardrails/network.py), which is
     what stops a click whose destination can't be known in advance — the
     request is aborted before it leaves the machine;
  3. after an action, on the URL the page actually landed on, which covers
     server-side redirects the request hook doesn't see.
"""
from __future__ import annotations

import os
from fnmatch import fnmatchcase
from typing import Iterable, Optional
from urllib.parse import urlparse

# Request types whose path is checked against `routes`. Images, styles and
# scripts are held to the domain allowlist only.
ROUTED_RESOURCE_TYPES = {"document", "xhr", "fetch"}


class AllowlistViolation(Exception):
    pass


def _split(raw: Optional[str]) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


class Allowlist:
    def __init__(self, domains: Iterable[str], routes: Optional[Iterable[str]] = None,
                 actions: Optional[Iterable[str]] = None, risky_routes: Optional[Iterable[str]] = None):
        self.domains = {d.strip() for d in domains if d.strip()}
        self.routes = [r for r in (routes or []) if r]
        self.actions = {a for a in (actions or []) if a}
        self.risky_routes = [r for r in (risky_routes or []) if r]

    @classmethod
    def from_env(cls) -> "Allowlist":
        return cls(
            domains=_split(os.environ.get("ALLOWLIST_DOMAINS")),
            routes=_split(os.environ.get("ALLOWLIST_ROUTES")),
            actions=_split(os.environ.get("ALLOWLIST_ACTIONS")),
            risky_routes=_split(os.environ.get("RISKY_ROUTES")),
        )

    def check_url(self, url: str, routed: bool = True) -> None:
        """Raise unless `url` is on an allowed host and, if `routed`, an
        allowed path. Anything that can't be parsed is refused."""
        try:
            parsed = urlparse(url)
            host, path = parsed.netloc, parsed.path or "/"
        except Exception as e:  # noqa: BLE001 - fail closed on anything unparseable
            raise AllowlistViolation(f"Could not parse URL {url!r} ({e}). Refusing to act.") from e
        if host not in self.domains:
            raise AllowlistViolation(
                f"URL host '{host}' (from {url}) is not in the configured allowlist "
                f"{sorted(self.domains)}. Refusing to act."
            )
        if routed and self.routes and not any(fnmatchcase(path, pattern) for pattern in self.routes):
            raise AllowlistViolation(
                f"Route '{path}' on '{host}' is not in the allowed routes {self.routes}. Refusing to act."
            )

    def check_request(self, url: str, resource_type: str) -> None:
        self.check_url(url, routed=resource_type in ROUTED_RESOURCE_TYPES)

    def check_action(self, action: str) -> None:
        if self.actions and action not in self.actions:
            raise AllowlistViolation(
                f"Action type '{action}' is not permitted by policy (allowed: {sorted(self.actions)}). "
                "Refusing to act."
            )

    def is_risky_request(self, method: str, url: str) -> bool:
        """A state-changing request to a route configured as risky. A GET is
        never risky by this rule: a passive read can't be irreversible."""
        if method.upper() == "GET" or not self.risky_routes:
            return False
        path = urlparse(url).path or "/"
        return any(fnmatchcase(path, pattern) for pattern in self.risky_routes)
