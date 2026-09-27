"""
Domain/route allowlist enforcement. Checked on every navigation and after
every action that might have caused one (a click can navigate), for both
discovery and replay — this is the one guardrail that must never be
bypassable by either code path.
"""
from __future__ import annotations

from urllib.parse import urlparse


class AllowlistViolation(Exception):
    pass


class Allowlist:
    def __init__(self, domains: list[str]):
        self.domains = {d.strip() for d in domains if d.strip()}

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        host = parsed.netloc
        if host not in self.domains:
            raise AllowlistViolation(
                f"URL host '{host}' (from {url}) is not in the configured allowlist "
                f"{sorted(self.domains)}. Refusing to act."
            )

    @classmethod
    def from_env(cls) -> "Allowlist":
        import os
        raw = os.environ.get("ALLOWLIST_DOMAINS", "")
        return cls(raw.split(","))
