"""
Keeps the Surface seam honest: the real replay engine runs a real artifact
on a surface that is not a browser at all. If the engine reached past the
interface into Playwright anywhere, these would fail.

`InMemoryConsole` is a twenty-line model of the member-lookup screens. It
stands in for "some other surface" (a desktop app, a terminal emulator): it
finds controls from the same LocatorSpec the web surface uses and knows
nothing about the DOM.

Run: pytest tests/test_surface_seam.py
"""
import subprocess
import sys
from pathlib import Path

import pytest  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artifacts.schema import ReplayOutcome  # noqa: E402
from guardrails.allowlist import Allowlist  # noqa: E402
from replay.executor import ReplayExecutor  # noqa: E402
from replay.surface import ResolvedTarget, Surface, SurfaceError, TargetNotFound  # noqa: E402
from tests.fixtures.example_artifact import build_lookup_member_balance_fixture  # noqa: E402

BASE = "console://core"
MEMBERS = {"10001": {"Name": "Alice Rivera", "Savings": "$8,150.32"}}


class InMemoryConsole(Surface):
    """Screens: search -> results | not-found -> detail."""

    def __init__(self, break_search: bool = False):
        self.screen, self.member_id, self.typed = "closed", None, ""
        self.break_search = break_search
        self.calls: list[str] = []

    # what is on each screen, keyed the way a LocatorStrategy names it
    def _controls(self) -> dict[str, str]:
        if self.screen == "search":
            return {"input[name='member_id']": "field", "Search": "button"}
        if self.screen == "results":
            return {"View Member": "button", "Search": "button"}
        if self.screen == "detail":
            row = MEMBERS[self.member_id]
            return {f"//tr[td[normalize-space()='Name']]/td[2]": row["Name"],
                    "//tr[td[1][normalize-space()='Savings']]/td[3]": row["Savings"]}
        return {}

    def _texts(self) -> list[str]:
        return ["No member found matching ID"] if self.screen == "not_found" else []

    def open(self): self.screen = "login"
    def close(self): self.screen = "closed"
    def sign_in(self, base_url): self.screen = "home"
    attach_endpoint = "console://core/attach"

    def navigate(self, location):
        self.calls.append(f"navigate {location}")
        self.screen = "search"

    def location(self):
        return f"{BASE}/members/{self.member_id}" if self.screen == "detail" else f"{BASE}/members/search"

    def reload(self): pass

    def resolve(self, spec, timeout_ms):
        attempts = []
        for index, strategy in enumerate(spec.strategies):
            key = strategy.role_name or strategy.value
            if key in self._controls():
                return ResolvedTarget(handle=key, strategy_index=index, method=strategy.method)
            attempts.append(f"[{index}] {key!r} -> not on the {self.screen} screen")
        raise TargetNotFound(spec, attempts)

    def is_present(self, spec, timeout_ms):
        return any(s.value in text for s in spec.strategies for text in self._texts())

    def text_visible(self, text, timeout_ms): return any(text in t for t in self._texts())

    def click(self, target):
        self.calls.append(f"click {target.handle}")
        if target.handle == "Search":
            if self.break_search:
                raise SurfaceError("ConsoleTimeout: the host did not respond")
            self.member_id = self.typed
            self.screen = "results" if self.typed in MEMBERS else "not_found"
        elif target.handle == "View Member":
            self.screen = "detail"

    def fill(self, target, value):
        self.calls.append(f"fill {target.handle}")
        self.typed = value

    def read_text(self, target): return self._controls()[target.handle]
    def expect_confirmation(self, answer): pass
    def begin_action(self, allow_risky=False): pass
    def check_action(self): pass
    def blocked_requests(self): return []
    def screenshot(self, path): return False
    def snapshot(self): return f"<console screen={self.screen}>"
    def begin_human_control(self, control, reason): pass
    def end_human_control(self): return []
    def idle(self, seconds): pass


def _run(member_id, tmp_path, **surface_kwargs):
    console = InMemoryConsole(**surface_kwargs)
    executor = ReplayExecutor(build_lookup_member_balance_fixture(base_url=BASE), tmp_path, surface=console)
    executor.allowlist = Allowlist(["core"])
    return console, executor.run({"member_id": member_id})


def test_the_engine_replays_an_artifact_on_a_non_browser_surface(tmp_path):
    console, result = _run("10001", tmp_path)
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.outputs == {"member_name": "Alice Rivera", "savings_balance": 8150.32}
    assert console.calls == ["navigate console://core/members/search", "fill input[name='member_id']",
                             "click Search", "click View Member"]
    print("PASS: same artifact, same engine, no browser ->", result.outputs)


def test_outcome_taxonomy_is_surface_independent(tmp_path):
    _, not_found = _run("99999", tmp_path)
    assert not_found.outcome == ReplayOutcome.BUSINESS_OUTCOME and not_found.business_outcome.code == "member_not_found"

    _, failed = _run("10001", tmp_path, break_search=True)
    assert failed.outcome == ReplayOutcome.FAILURE, failed.model_dump()
    assert failed.failure.step_id == "s3" and "ConsoleTimeout" in failed.failure.observed
    assert (tmp_path / failed.run_id / failed.failure.dom_snapshot).read_text().startswith("<console screen=")
    print("PASS: business outcome and structured failure come from the engine, not the driver")


def test_replay_engine_has_no_driver_or_model_dependency():
    """Importing the engine must not pull in Playwright, the discovery
    agent, or any model client."""
    code = ("import sys, replay.executor; "
            "bad = [m for m in sys.modules if m.split('.')[0] in ('playwright', 'agent', 'google', 'httpx')]; "
            "print(','.join(sorted(set(m.split('.')[0] for m in bad))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parent.parent)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"replay.executor imports: {out.stdout.strip()}"
    print("PASS: replay.executor imports no browser driver, no agent code, no model client")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
