"""
Enforces the allowlist on the wire. A click's destination can't be known
before it happens, so checking the page URL afterwards is too late — the
request has already reached the server. This hooks every request the session
makes and aborts the ones policy forbids before they are sent.

Two decisions per request:

  - Off the allowlist (wrong host, or a document/XHR to a route that isn't
    permitted): aborted, always.
  - A state-changing request to a risky route: allowed only if the action
    in progress was cleared for it (`begin(allow_risky=True)`) — because a
    human confirmed it, performed it, or reviewed the artifact that contains
    it. Otherwise aborted. This is the backstop behind step-level risk
    flags: an action nobody classified as risky still can't commit one.

Usage: call `begin()` before an action, then `raise_if_blocked()` after it.
Anything the hook can't evaluate is aborted (fail closed).

While a human operator holds the session the same hook stays in force:
they can perform risky actions (that is why they were brought in) but they
can't take the session off the allowlist either.

Limit: Playwright does not call the hook again for redirect hops, so the
post-action URL check in the callers remains the backstop for redirects.
"""
from __future__ import annotations

from guardrails.allowlist import Allowlist, AllowlistViolation

_LOCAL_SCHEMES = ("data:", "about:", "blob:", "chrome-error:")


class RiskyActionBlocked(Exception):
    """An action that was not cleared as risky tried to make a state-changing
    request to a risky route. The request was aborted; nothing was committed."""


class NetworkGuard:
    def __init__(self, allowlist: Allowlist):
        self.allowlist = allowlist
        self._allow_risky = False
        self.violations: list[str] = []      # off-allowlist requests aborted during this action
        self.risky_blocked: list[str] = []   # risky requests aborted during this action
        self.risky_allowed: list[str] = []   # risky requests let through during this action

    def install(self, context) -> None:
        context.route("**/*", self._on_request)

    def begin(self, allow_risky: bool = False) -> None:
        self._allow_risky = allow_risky
        self.violations, self.risky_blocked, self.risky_allowed = [], [], []

    def _on_request(self, route) -> None:
        try:
            request = route.request
            url = request.url
            if url.startswith(_LOCAL_SCHEMES):
                route.continue_()
                return
            self.allowlist.check_request(url, request.resource_type)
            if self.allowlist.is_risky_request(request.method, url):
                label = f"{request.method} {url}"
                if not self._allow_risky:
                    self.risky_blocked.append(label)
                    route.abort("aborted")
                    return
                self.risky_allowed.append(label)
            route.continue_()
        except AllowlistViolation as e:
            self.violations.append(str(e))
            route.abort("aborted")
        except Exception as e:  # noqa: BLE001 - a hook that can't decide must not let the request through
            self.violations.append(f"Request could not be evaluated against policy ({type(e).__name__}: {e}).")
            try:
                route.abort("aborted")
            except Exception:  # noqa: BLE001
                pass

    def raise_if_blocked(self) -> None:
        if self.violations:
            raise AllowlistViolation(self.violations[0] + " The request was aborted before it was sent.")
        if self.risky_blocked:
            raise RiskyActionBlocked(
                f"This action tried to make a risky, state-changing request ({self.risky_blocked[0]}) "
                "without being cleared for it. The request was aborted before it was sent."
            )
