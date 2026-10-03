"""
Exercises the replay engine against every runtime condition the target app
is seeded to reproduce: happy path, a named business outcome, a recoverable
interstitial, and an escalation that gets resolved by a simulated operator
reattaching to the live session via CDP. None of this needs an LLM or an
API key — replay never calls one.

Run: pytest tests/test_replay_scenarios.py
"""
import pytest  # noqa: F401
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
    ArtifactStatus,
    LocatorMethod,
    LocatorSpec,
    LocatorStrategy,
    ReplayOutcome,
    Step,
)
from guardrails.allowlist import Allowlist  # noqa: E402
from artifacts.review import approve  # noqa: E402

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
    # What the human did is observed off the live page, not self-reported.
    observed = [a for a in result.escalation.human_actions if a["source"] == "observed"]
    assert any(a["kind"] == "click" and a.get("text") == "Confirm & Open Account" for a in observed), observed
    assert any(a["kind"] == "dialog" for a in observed), observed
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
    """'takeover': an operator can pause and grab control on ANY
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
    """'cancel': unblocks a stuck escalation without waiting
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
    """'interrupt': ends the run outright, at any step boundary
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
    approve(artifact, "test")  # content changed, so it needs a fresh approval to run
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
    approve(artifact, "test")
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
    approve(artifact, "test")
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
        return approve(artifact, "test")

    ok = ReplayExecutor(with_assert("Alice Rivera"), fresh_evidence_root(), headless=True).run({"member_id": "10001"})
    assert ok.outcome == ReplayOutcome.SUCCESS, ok.model_dump()
    bad = ReplayExecutor(with_assert("Text That Is Not There"), fresh_evidence_root(), headless=True).run(
        {"member_id": "10001"})
    assert bad.outcome == ReplayOutcome.FAILURE, bad.model_dump()
    assert bad.failure.step_id == "s_assert", bad.failure
    print("PASS: assert_text step ->", ok.outcome, "/", bad.outcome, bad.failure.observed[:60])


def _break_search_step(artifact):
    step = artifact.steps[2]  # the Search click
    step.timeout_ms = 500
    step.target = LocatorSpec(strategies=[LocatorStrategy(
        method=LocatorMethod.ROLE, value="button", role_name="No Such Button", reasoning="test: cannot resolve")])
    return approve(artifact, "test"), step


def test_failure_escalates_and_human_completes_the_step():
    """With escalate_on_failure, a step replay can't perform is handed to a
    human on the same live session. They do it, say so, and automation
    carries on from the next step and still verifies the checkpoint."""
    artifact, step = _break_search_step(build_lookup_member_balance_fixture())
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)

    def on_escalation(control, ctx):
        assert ctx["kind"] == "failure", ctx
        threading.Thread(target=simulate_operator_takeover, args=(control,),
                         kwargs={"actions": [("click", "Search")], "step_done": True,
                                 "reaction_delay_s": 0.3}, daemon=True).start()

    result = ex.run({"member_id": "10001"}, escalate_on_failure=True, on_escalation=on_escalation)
    assert result.outcome == ReplayOutcome.SUCCESS, result.model_dump()
    assert result.outputs["savings_balance"] == 8150.32, result.outputs
    assert result.escalation.kind == "failure" and result.escalation.step_id == step.step_id
    assert "No Such Button" in result.escalation.reason, result.escalation.reason
    observed = [a["description"] for a in result.escalation.human_actions if a["source"] == "observed"]
    assert "Clicked 'Search' button" in observed, observed
    print("PASS: failed step escalated, human completed it ->", result.outcome, observed)


def test_failure_escalation_handed_back_unfixed_is_hard_failure():
    """If the human hands the step back and it still can't run, that is a
    hard failure — the step is escalated once, not in a loop."""
    artifact, step = _break_search_step(build_lookup_member_balance_fixture())
    ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)

    def on_escalation(control, ctx):
        threading.Thread(target=simulate_operator_takeover, args=(control,),
                         kwargs={"step_done": False, "reaction_delay_s": 0.3}, daemon=True).start()

    result = ex.run({"member_id": "10001"}, escalate_on_failure=True, on_escalation=on_escalation)
    assert result.outcome == ReplayOutcome.FAILURE, result.model_dump()
    assert result.failure.step_id == step.step_id and result.escalation.kind == "failure"
    print("PASS: handed back unfixed -> hard FAILURE ->", result.failure.step_id)


def _as_draft(artifact):
    artifact.status = ArtifactStatus.DRAFT
    artifact.review = None
    return artifact


def test_draft_is_refused_unattended_and_runs_attended():
    """A discovery output is a draft. It never replays unattended; a person
    validating it can run it attended. The refusal happens before a browser
    opens and still leaves a result.json behind."""
    ex = ReplayExecutor(_as_draft(build_lookup_member_balance_fixture()), fresh_evidence_root(), headless=True)
    refused = ex.run({"member_id": "10001"})
    assert refused.outcome == ReplayOutcome.REFUSED, refused.model_dump()
    assert refused.steps_executed == 0 and "draft" in refused.failure.observed
    assert (ex.evidence_dir / "result.json").exists()

    ex = ReplayExecutor(_as_draft(build_lookup_member_balance_fixture()), fresh_evidence_root(), headless=True)
    attended = ex.run({"member_id": "10001"}, attended=True)
    assert attended.outcome == ReplayOutcome.SUCCESS, attended.model_dump()
    print("PASS: draft refused unattended, runs attended ->", refused.outcome, "/", attended.outcome)


def test_draft_cannot_auto_approve_irreversible_step():
    """auto_approve means 'the artifact's review is the confirmation'. A
    draft has no review, so even attended it can't skip the human."""
    ex = ReplayExecutor(_as_draft(build_open_sub_account_fixture()), fresh_evidence_root(), headless=True)
    result = ex.run({"member_id": "10002", "nickname": "Rainy Day", "initial_deposit": "100"},
                    auto_approve=True, attended=True)
    assert result.outcome == ReplayOutcome.REFUSED, result.model_dump()
    assert "auto-approved" in result.failure.observed, result.failure
    print("PASS: draft + auto_approve refused ->", result.outcome)


def test_edit_after_approval_voids_it():
    """Approval is bound to a hash of what was reviewed. Changing a step
    afterwards voids it, attended or not."""
    artifact = build_lookup_member_balance_fixture()
    artifact.steps[2].target.strategies[0].role_name = "Delete Member"
    for attended in (False, True):
        ex = ReplayExecutor(artifact, fresh_evidence_root(), headless=True)
        result = ex.run({"member_id": "10001"}, attended=attended)
        assert result.outcome == ReplayOutcome.REFUSED, result.model_dump()
        assert "changed since it was reviewed" in result.failure.observed, result.failure
    print("PASS: edit after approval voids the approval ->", result.outcome)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
