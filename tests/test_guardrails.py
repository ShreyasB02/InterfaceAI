"""
Exercises the safety guardrails: the allowlist policy (domains, routes,
action types), its enforcement on the wire, the risky-route backstop, and
redaction of logs and artifact text.

The first group needs nothing running. The second drives the replay engine
against the target app (python target_app/app.py). No LLM, no API key.

Run: pytest tests/test_guardrails.py
"""
import pytest  # noqa: F401
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ALLOWLIST_DOMAINS", "127.0.0.1:5055")

from artifacts.review import approve  # noqa: E402
from artifacts.schema import (  # noqa: E402
    ActionType,
    LocatorMethod,
    LocatorSpec,
    LocatorStrategy,
    ReplayOutcome,
    RiskLevel,
    Step,
)
from guardrails.allowlist import Allowlist, AllowlistViolation  # noqa: E402
from guardrails.redaction import generalize_for_artifact, redact_dict  # noqa: E402
from replay.executor import ReplayExecutor  # noqa: E402
from tests.fixtures.example_artifact import (  # noqa: E402
    build_lookup_member_balance_fixture,
    build_open_sub_account_fixture,
)

EVIDENCE_ROOT = Path("/tmp/cua_guardrails_test_evidence")
HOST = "127.0.0.1:5055"
APP_ROUTES = ["/", "/login", "/members/*"]


def _refused(fn) -> str:
    try:
        fn()
    except AllowlistViolation as e:
        return str(e)
    raise AssertionError("expected AllowlistViolation")


# -- policy, no browser ------------------------------------------------------

def test_policy_axes():
    policy = Allowlist([HOST], routes=APP_ROUTES, actions=["navigate", "click", "extract"],
                       risky_routes=["*/confirm"])
    policy.check_url(f"http://{HOST}/members/10001")
    assert "host" in _refused(lambda: policy.check_url("http://evil.example/members/10001"))
    assert "Route '/logout'" in _refused(lambda: policy.check_url(f"http://{HOST}/logout"))
    # Static assets are held to the domain only, not the route list.
    policy.check_request(f"http://{HOST}/static/app.css", "stylesheet")
    assert "Route" in _refused(lambda: policy.check_request(f"http://{HOST}/admin/export", "xhr"))

    policy.check_action("click")
    assert "'fill' is not permitted" in _refused(lambda: policy.check_action("fill"))

    confirm = f"http://{HOST}/members/10001/open-subaccount/confirm"
    assert policy.is_risky_request("POST", confirm)
    assert not policy.is_risky_request("GET", confirm), "a read is never risky"
    assert not policy.is_risky_request("POST", f"http://{HOST}/members/search")
    print("PASS: policy axes — domains, routes, action types, risky routes")


def test_policy_fails_closed():
    assert "host" in _refused(lambda: Allowlist([]).check_url(f"http://{HOST}/"))
    assert _refused(lambda: Allowlist([HOST]).check_url("not a url at all"))
    assert _refused(lambda: Allowlist([HOST]).check_url("javascript:alert(1)"))
    print("PASS: an empty or unparseable policy input blocks, never allows")


def test_log_redaction_is_recursive():
    event = redact_dict({
        "event": "decide", "assistant_text": "Balance is $8,150.32 on SAV-10001-01",
        "tool_input": {"passcode": "hunter2", "nested": {"note": "ssn 123-45-6789"}},
        "human_actions": [{"description": "typed $40.00"}],
    })
    flat = json.dumps(event)
    for leaked in ("8,150.32", "SAV-10001-01", "hunter2", "123-45-6789", "40.00"):
        assert leaked not in flat, (leaked, flat)
    print("PASS: log redaction reaches every field at every depth")


def test_artifact_text_is_generalized():
    # The actual text a real model wrote in this repo's first discovery run.
    text = ("The Member Detail page for member 10001 is visible with Accounts table displaying "
            "Savings account SAV-10001-01 and balance $8150.32. Looked up Alice Rivera.")
    out = generalize_for_artifact(text, {"member_id": "10001"},
                                  {"member_name": "Alice Rivera", "savings_balance": "$8,150.32"})
    for leaked in ("10001", "SAV-", "8150", "Alice", "Rivera"):
        assert leaked not in out, (leaked, out)
    assert "{member_id}" in out and "{member_name}" in out, out
    print("PASS: artifact text carries placeholders, not the record it was recorded on ->", out)


# -- enforcement, against the live app --------------------------------------

def _run(artifact, policy, params, **kw):
    if EVIDENCE_ROOT.exists():
        shutil.rmtree(EVIDENCE_ROOT)
    ex = ReplayExecutor(artifact, EVIDENCE_ROOT, headless=True)
    ex.allowlist = policy
    return ex, ex.run(params, **kw)


def _events(ex) -> list[dict]:
    return [json.loads(line) for line in (ex.evidence_dir / "log.jsonl").read_text().splitlines()]


def test_route_policy_does_not_break_a_legitimate_run():
    policy = Allowlist([HOST], routes=APP_ROUTES, risky_routes=["*/confirm"])
    ex, result = _run(build_lookup_member_balance_fixture(), policy, {"member_id": "10001"})
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    print("PASS: a run inside the policy is unaffected ->", result.outputs)


def test_click_to_forbidden_route_is_aborted_on_the_wire():
    """A click's destination isn't known in advance. 'Log Off' POSTs to
    /logout, which the route policy doesn't permit: the request is aborted
    before it is sent, so the session is still logged in afterwards."""
    artifact = build_lookup_member_balance_fixture()
    artifact.steps.append(Step(
        step_id="s_logoff", intent="Click 'Log Off'", action=ActionType.CLICK,
        target=LocatorSpec(strategies=[LocatorStrategy(
            method=LocatorMethod.ROLE, value="button", role_name="Log Off", reasoning="test")])))
    artifact.steps.append(Step(
        step_id="s_after", intent="Still on the member page, still signed in.",
        action=ActionType.ASSERT_TEXT, value_literal="Alice Rivera", timeout_ms=1000))
    approve(artifact, "test")

    policy = Allowlist([HOST], routes=["/", "/login", "/members/*"])
    ex, result = _run(artifact, policy, {"member_id": "10001"})
    assert result.outcome == ReplayOutcome.FAILURE, result.model_dump()
    assert result.failure.step_id == "s_logoff", result.failure
    assert "/logout" in result.failure.observed and "aborted before it was sent" in result.failure.observed
    # The DOM snapshot at failure is the member page, not the login page.
    dom = (ex.evidence_dir / result.failure.dom_snapshot).read_text()
    assert "Alice Rivera" in dom and "Log Off" in dom, "session should still be signed in on the member page"
    print("PASS: forbidden-route click aborted before it left ->", result.failure.observed[:90])


def test_action_type_policy():
    """A read-only policy: no `fill`. The run stops at the fill step before
    touching the field; login (infrastructure) is unaffected."""
    policy = Allowlist([HOST], actions=["navigate", "click", "wait_for", "extract"])
    ex, result = _run(build_lookup_member_balance_fixture(), policy, {"member_id": "10001"})
    assert result.outcome == ReplayOutcome.FAILURE, result.model_dump()
    assert result.failure.step_id == "s2" and "'fill' is not permitted" in result.failure.observed
    assert result.steps_executed == 1
    print("PASS: disallowed action type blocked before it ran ->", result.failure.step_id)


def test_navigation_is_checked_before_leaving():
    artifact = build_lookup_member_balance_fixture()
    artifact.steps[0].value_literal = "/admin/export"
    approve(artifact, "test")
    ex, result = _run(artifact, Allowlist([HOST], routes=APP_ROUTES), {"member_id": "10001"})
    assert result.outcome == ReplayOutcome.FAILURE and result.failure.step_id == "s1", result.model_dump()
    assert "Route '/admin/export'" in result.failure.observed
    print("PASS: off-policy navigation refused before the request ->", result.failure.observed[:70])


def test_under_classified_step_cannot_commit_a_risky_request():
    """Backstop behind step-level risk flags. An artifact that (wrongly)
    records the confirm click as safe still can't commit it: the POST to a
    risky route is aborted because nothing cleared that step for it."""
    params = {"member_id": "10002", "nickname": "Rainy Day", "initial_deposit": "100"}
    policy = Allowlist([HOST], routes=APP_ROUTES, risky_routes=["*/confirm"])

    wrong = build_open_sub_account_fixture()
    for step in wrong.steps:
        step.risk_level, step.requires_confirmation = RiskLevel.SAFE, False
    approve(wrong, "careless-reviewer")
    ex, result = _run(wrong, policy, params)
    assert result.outcome == ReplayOutcome.FAILURE, result.model_dump()
    assert "risky_request_blocked" in [e["event"] for e in _events(ex)]
    assert "open-subaccount/confirm" in result.failure.observed, result.failure

    # The correctly classified artifact, with its review standing as the
    # confirmation, is cleared for exactly that request.
    ex, result = _run(build_open_sub_account_fixture(), policy, params, auto_approve=True)
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    print("PASS: risky request blocked for an unflagged step, allowed for a cleared one ->",
          result.outputs)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
