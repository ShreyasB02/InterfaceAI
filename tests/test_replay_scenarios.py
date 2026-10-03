"""
Exercises the replay engine against every runtime condition the target app
is seeded to reproduce: happy path, a named business outcome, a recoverable
interstitial, and an escalation that gets resolved by a simulated operator
reattaching to the live session via CDP. None of this needs an LLM or an
API key — replay never calls one.

Run: python3 tests/test_replay_scenarios.py
"""
import shutil
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402
os.environ.setdefault("ALLOWLIST_DOMAINS", "127.0.0.1:5055")

from tests.fixtures.example_artifact import (  # noqa: E402
    build_lookup_member_balance_fixture,
    build_open_sub_account_fixture,
)
from replay.executor import ReplayExecutor  # noqa: E402
from escalation.control_channel import ControlChannel  # noqa: E402
from escalation.simulated_operator import simulate_operator_takeover  # noqa: E402
from artifacts.schema import (  # noqa: E402
    ActionType,
    LocatorMethod,
    LocatorSpec,
    LocatorStrategy,
    ReplayOutcome,
    Step,
)
from guardrails.allowlist import Allowlist  # noqa: E402

EVIDENCE_ROOT = Path("/tmp/cua_replay_test_evidence")


def fresh_evidence_root():
    if EVIDENCE_ROOT.exists():
        shutil.rmtree(EVIDENCE_ROOT)
    return EVIDENCE_ROOT


def test_happy_path():
    artifact = build_lookup_member_balance_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10001"})
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.outputs["member_name"] == "Alice Rivera", result.outputs
    assert result.outputs["savings_balance"] == 8150.32, result.outputs
    print("PASS: happy path ->", result.outcome, result.outputs)


def test_business_outcome_not_found():
    artifact = build_lookup_member_balance_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "99999"})
    assert result.outcome == ReplayOutcome.BUSINESS_OUTCOME, result.model_dump()
    assert result.business_outcome.code == "member_not_found", result.business_outcome
    print("PASS: business outcome (not found) ->", result.outcome, result.business_outcome)


def test_recoverable_flaky_session():
    artifact = build_lookup_member_balance_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10004"})
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert len(result.recovered_steps) == 1, result.recovered_steps
    assert "session" in result.recovered_steps[0].condition.lower()
    assert result.outputs["member_name"] == "Dana Kim"
    print("PASS: recoverable session-expired ->", result.outcome, result.recovered_steps)


def test_business_outcome_permission_denied():
    artifact = build_open_sub_account_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10003", "nickname": "Test", "initial_deposit": "100"})
    assert result.outcome == ReplayOutcome.BUSINESS_OUTCOME, result.model_dump()
    assert result.business_outcome.code == "permission_denied", result.business_outcome
    print("PASS: business outcome (permission denied) ->", result.outcome, result.business_outcome)


def test_business_outcome_invalid_deposit():
    artifact = build_open_sub_account_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10001", "nickname": "Test", "initial_deposit": "5"})
    assert result.outcome == ReplayOutcome.BUSINESS_OUTCOME, result.model_dump()
    assert result.business_outcome.code == "invalid_deposit_amount", result.business_outcome
    print("PASS: business outcome (invalid deposit) ->", result.outcome, result.business_outcome)


def test_escalation_resolved_by_simulated_operator():
    artifact = build_open_sub_account_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)

    def on_escalation(control, ctx):
        t = threading.Thread(
            target=simulate_operator_takeover,
            args=(control, "Confirm & Open Account"),
            kwargs={"accept_dialog": True, "reaction_delay_s": 0.5},
            daemon=True,
        )
        t.start()

    result = ex.run(
        {"member_id": "10001", "nickname": "Vacation Fund", "initial_deposit": "150"},
        auto_approve=False,
        escalation_timeout_s=15,
        on_escalation=on_escalation,
    )
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.escalation is not None, "expected escalation to be recorded even though it resolved"
    assert result.escalation.step_id == "s9"
    assert result.outputs["new_account_number"].startswith("SUB-10001-")
    print("PASS: escalation resolved by simulated operator ->", result.outcome, result.escalation, result.outputs)


def test_escalation_auto_approve_bypass():
    artifact = build_open_sub_account_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run(
        {"member_id": "10002", "nickname": "Auto Approved", "initial_deposit": "75"},
        auto_approve=True,
    )
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.escalation is None
    print("PASS: auto-approve bypass ->", result.outcome, result.outputs)


def test_manual_takeover_on_non_risky_step():
    """Tier 2 #7's 'takeover': an operator can pause and grab control on ANY
    step, not just one the artifact flagged risky — the lookup fixture has
    no risky/requires_confirmation steps at all, and this still escalates
    on its very first (NAVIGATE) step because the takeover signal is set
    before the run even starts. Unlike a risk-triggered escalation, the
    step itself is NOT skipped after resume — automation executes it
    itself once control is handed back (see replay/executor.py)."""
    artifact = build_lookup_member_balance_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    # Constructed against the same run_id/evidence_dir the executor will use
    # internally — same file, same cross-process coordination the real
    # operator console and CDP simulator rely on (see control_channel.py).
    pre_control = ControlChannel(ex.run_id, ex.evidence_dir)
    pre_control.request_takeover(reason="test: operator wants to look around first")

    def on_escalation(control, ctx):
        def _resume():
            control.mark_human_active()
            control.record_human_action("looked around, nothing needed")
            control.signal_resume()
        threading.Thread(target=_resume, daemon=True).start()

    result = ex.run({"member_id": "10001"}, escalation_timeout_s=10, on_escalation=on_escalation)
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.escalation is not None, "expected the takeover to be recorded as an escalation"
    assert result.escalation.step_id == "s1", result.escalation
    assert result.outputs["member_name"] == "Alice Rivera", result.outputs
    assert result.steps_executed == 6, "every step should still have run — takeover doesn't skip its step"
    print("PASS: manual takeover on a non-risky step ->", result.outcome, result.escalation)


def test_escalation_cancelled_reports_failure():
    """Tier 2 #7's 'cancel': unblocks a stuck escalation without waiting
    for a timeout, and is reported as a controlled FAILURE rather than
    hanging forever or being silently retried."""
    artifact = build_open_sub_account_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)

    def on_escalation(control, ctx):
        threading.Thread(
            target=lambda: control.cancel(reason="test: operator gave up on this one"),
            daemon=True,
        ).start()

    result = ex.run(
        {"member_id": "10001", "nickname": "Vacation Fund", "initial_deposit": "150"},
        auto_approve=False, escalation_timeout_s=15, on_escalation=on_escalation,
    )
    assert result.outcome == ReplayOutcome.FAILURE, result.model_dump()
    assert result.failure is not None
    assert result.failure.step_id == "s9", result.failure
    assert "cancel" in result.failure.observed.lower(), result.failure
    print("PASS: escalation cancelled -> reported as FAILURE ->", result.outcome, result.failure)


def test_run_interrupted_mid_automation():
    """Tier 2 #7's 'interrupt': ends the run outright, at any step boundary
    — not only while an escalation is pending. RunInterrupted is raised as
    a BaseException (see escalation/transport.py) and is caught explicitly
    by replay/executor.py's own run(), converting it into a clean
    ReplayOutcome.INTERRUPTED result rather than propagating out raw or
    being mistaken for a code failure by a broad except Exception."""
    artifact = build_lookup_member_balance_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    pre_control = ControlChannel(ex.run_id, ex.evidence_dir)
    pre_control.interrupt(reason="test: operator stopped the run")

    result = ex.run({"member_id": "10001"})
    assert result.outcome == ReplayOutcome.INTERRUPTED, result.model_dump()
    assert result.interrupted is not None
    assert result.interrupted.step_id == "s1", result.interrupted
    assert result.interrupted.reason == "test: operator stopped the run"
    assert result.steps_executed == 0, "no step should have executed once the interrupt was seen"
    print("PASS: run interrupted mid-automation ->", result.outcome, result.interrupted)


def test_hard_failure_unresolvable_locator():
    """A control that cannot be found is a hard FAILURE carrying step /
    expected / observed plus a screenshot and a DOM snapshot — not a crash."""
    artifact = build_lookup_member_balance_fixture()
    step = artifact.steps[2]  # the Search click
    step.timeout_ms = 500
    step.target = LocatorSpec(strategies=[LocatorStrategy(
        method=LocatorMethod.ROLE, value="button", role_name="No Such Button", reasoning="test: cannot resolve")])
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10001"})
    assert result.outcome == ReplayOutcome.FAILURE, result.model_dump()
    assert result.failure.step_id == step.step_id, result.failure
    assert "No Such Button" in result.failure.expected, result.failure
    assert (ex.evidence_dir / result.failure.screenshot).exists(), result.failure
    assert (ex.evidence_dir / result.failure.dom_snapshot).exists(), result.failure
    print("PASS: hard failure (unresolvable locator) ->", result.outcome, result.failure.step_id)


def test_hard_failure_target_app_unreachable():
    """The app being down fails the login bootstrap. That must come back as
    a structured FAILURE, not a raw Playwright traceback."""
    artifact = build_lookup_member_balance_fixture()
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    ex.allowlist = Allowlist(["127.0.0.1:5999"])
    result = ex.run({"member_id": "10001"}, base_url="http://127.0.0.1:5999")
    assert result.outcome == ReplayOutcome.FAILURE, result.model_dump()
    assert result.failure.step_id == "session_bootstrap", result.failure
    assert result.steps_executed == 0
    print("PASS: hard failure (app unreachable) ->", result.outcome, result.failure.observed[:80])


def test_missing_declared_output_is_failure():
    """SUCCESS promises every declared output. Dropping an extract step must
    not yield a success with a hole in it."""
    artifact = build_lookup_member_balance_fixture()
    artifact.steps = [s for s in artifact.steps if s.output_name != "savings_balance"]
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10001"})
    assert result.outcome == ReplayOutcome.FAILURE, result.model_dump()
    assert result.failure.step_id == "outputs", result.failure
    assert "savings_balance" in result.failure.observed, result.failure
    print("PASS: missing declared output -> FAILURE ->", result.failure.observed)


def test_locator_fallback_is_reported():
    """When the primary strategy stops resolving and a lower-ranked one is
    used, the run still succeeds but reports it — the drift signal."""
    artifact = build_lookup_member_balance_fixture()
    step = artifact.steps[2]  # the Search click
    step.timeout_ms = 500
    step.target.strategies.insert(0, LocatorStrategy(
        method=LocatorMethod.CSS, value="button#renamed-by-vendor", reasoning="test: drifted primary"))
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10001"})
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert [f.step_id for f in result.locator_fallbacks] == [step.step_id], result.locator_fallbacks
    assert result.locator_fallbacks[0].strategy_index == 1
    print("PASS: locator fallback reported ->", result.locator_fallbacks)


def test_assert_text_step():
    """ASSERT_TEXT is a real mid-flow checkpoint: passes when the text is
    there, hard-fails with expected/observed when it is not."""
    def with_assert(text):
        artifact = build_lookup_member_balance_fixture()
        artifact.steps.append(Step(step_id="s_assert", intent="Confirm the member detail page is showing.",
                                   action=ActionType.ASSERT_TEXT, value_literal=text, timeout_ms=500))
        return artifact

    ok = ReplayExecutor(with_assert("Alice Rivera"), fresh_evidence_root(), headless=True).run({"member_id": "10001"})
    assert ok.outcome == ReplayOutcome.SUCCESS, ok.model_dump()
    bad = ReplayExecutor(with_assert("Text That Is Not There"), fresh_evidence_root(), headless=True).run(
        {"member_id": "10001"})
    assert bad.outcome == ReplayOutcome.FAILURE, bad.model_dump()
    assert bad.failure.step_id == "s_assert", bad.failure
    print("PASS: assert_text step ->", ok.outcome, "/", bad.outcome, bad.failure.observed[:60])


if __name__ == "__main__":
    test_happy_path()
    test_business_outcome_not_found()
    test_recoverable_flaky_session()
    test_business_outcome_permission_denied()
    test_business_outcome_invalid_deposit()
    test_escalation_resolved_by_simulated_operator()
    test_escalation_auto_approve_bypass()
    test_manual_takeover_on_non_risky_step()
    test_escalation_cancelled_reports_failure()
    test_run_interrupted_mid_automation()
    test_hard_failure_unresolvable_locator()
    test_hard_failure_target_app_unreachable()
    test_missing_declared_output_is_failure()
    test_locator_fallback_is_reported()
    test_assert_text_step()
    print("\nALL REPLAY SCENARIO TESTS PASSED")
